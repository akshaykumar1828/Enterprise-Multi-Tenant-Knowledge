"""Authentication: JWT access tokens and the current-user dependency.

Flow:
    POST /api/v1/auth/register  -> creates a new tenant (organization) and its first user
    POST /api/v1/auth/login     -> email + password -> short-lived signed access token (HS256)
    Authorization: Bearer <token> on protected routes -> get_current_user()

The token carries only the user id. The user, and with it the tenant, role and
departments (the AccessScope), is loaded from the database on every request,
so permissions always come from the server side, never from anything the
client sends.
"""

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.rag.access import AccessScope, load_access_scope
from src.rag.db import connect_app
from src.rag.users import User, authenticate, get_user, register_organization

from .rate_limit import client_ip, email_key, enforce
from .schemas import CurrentUserResponse, DepartmentInfo, LoginRequest, RegisterRequest, TokenResponse, UserResponse
from .settings import registration_open

ALGORITHM = "HS256"
TOKEN_TYPE = "access"
MIN_SECRET_LENGTH = 32


class AuthError(Exception):
    """Turned into 401 + WWW-Authenticate: Bearer by the app's exception handler."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def secret_key() -> str:
    key = os.environ.get("JWT_SECRET_KEY", "")
    if len(key) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET_KEY must be set (in .env) to a random string of at least {MIN_SECRET_LENGTH} characters"
        )
    return key


def token_lifetime() -> timedelta:
    return timedelta(minutes=int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60")))


def create_access_token(user_id: int) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    lifetime = token_lifetime()
    claims = {"sub": str(user_id), "typ": TOKEN_TYPE, "iat": now, "exp": now + lifetime}
    return jwt.encode(claims, secret_key(), algorithm=ALGORITHM), int(lifetime.total_seconds())


def user_id_from_token(token: str) -> int:
    try:
        # Pinning the algorithm list blocks "alg: none" and algorithm-confusion tokens.
        claims = jwt.decode(token, secret_key(), algorithms=[ALGORITHM], options={"require": ["exp", "iat", "sub"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("token_expired", "The access token has expired; log in again.") from None
    except jwt.InvalidTokenError:
        raise AuthError("invalid_token", "The access token is invalid.") from None
    if claims.get("typ") != TOKEN_TYPE or not str(claims["sub"]).isdigit():
        raise AuthError("invalid_token", "The access token is invalid.")
    return int(claims["sub"])


bearer_scheme = HTTPBearer(auto_error=False, description="Access token from POST /api/v1/auth/login")


@dataclass(frozen=True)
class AuthenticatedUser(User):
    """The requesting user plus their access scope, both loaded from the database for this request."""

    scope: AccessScope


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> AuthenticatedUser:
    if credentials is None:
        raise AuthError("not_authenticated", "Missing bearer token.")
    user_id = user_id_from_token(credentials.credentials)
    with connect_app() as conn:
        user = get_user(conn, user_id)
        # Role and departments are read fresh on every request; token claims are never used for them.
        scope = load_access_scope(conn, user_id) if user is not None else None
    if user is None or scope is None or scope.tenant_id != user.tenant_id:
        raise AuthError("invalid_token", "The access token is invalid.")
    return AuthenticatedUser(**asdict(user), scope=scope)


# --- routes ------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegistrationClosed(Exception):
    """Turned into 403 by the app's exception handler."""


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request) -> UserResponse:
    """Create a new organization (tenant) with this user as its first member."""
    if not registration_open():
        raise RegistrationClosed()
    enforce("REGISTER", f"register:{client_ip(request)}", "Too many registration attempts. Please try again later.")
    with connect_app() as conn:
        user = register_organization(conn, body.organization_name, body.email, body.password, body.display_name)
    return UserResponse.from_user(user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    # Every attempt counts (successful or not), per client IP and email.
    enforce("LOGIN", f"login:{client_ip(request)}:{email_key(body.email)}",
            "Too many login attempts. Please wait before trying again.")
    with connect_app() as conn:
        user = authenticate(conn, body.email, body.password)
    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=CurrentUserResponse)
def me(user: AuthenticatedUser = Depends(get_current_user)) -> CurrentUserResponse:
    # Role and departments come from the scope loaded for this request (the database).
    with connect_app() as conn:
        departments = conn.execute(
            "SELECT id, slug, name FROM departments WHERE tenant_id = %s AND id = ANY(%s::bigint[]) ORDER BY name, id",
            (user.scope.tenant_id, list(user.scope.department_ids)),
        ).fetchall()
    return CurrentUserResponse(
        **UserResponse.from_user(user).model_dump(),
        role=user.scope.role,
        departments=[DepartmentInfo(id=id_, slug=slug, name=name) for id_, slug, name in departments],
    )
