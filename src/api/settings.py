"""Deployment settings for the API.

APP_ENV=production turns on production behavior:
  - /docs, /redoc and /openapi.json are not served
  - /api/v1/health returns only an overall status (no corpus counts, models or devices)
  - startup refuses to run unless the API connects with a least-privilege database
    role (APP_DB_USER): not a superuser and not the owner of any application table

Anything else (the default) is development: everything behaves as before.

Limits (all from the environment; validated at startup):
  REGISTRATION_MODE      open | closed       default: closed in production, open otherwise
  RATE_LIMITS_ENABLED    true | false        default: true in production, false otherwise
  RATE_LIMIT_LOGIN       per IP + email      default 5/minute
  RATE_LIMIT_REGISTER    per IP              default 3/hour
  RATE_LIMIT_QUERIES     per tenant          default 30/minute   (every /query)
  RATE_LIMIT_ANSWERS     per tenant          default 20/day      (queries that call Gemini)
  RATE_LIMIT_UPLOADS     per tenant          default 20/hour
Format "<count>/<second|minute|hour|day>"; "off" disables one limit.
"""

import os
import re
from dataclasses import dataclass

import psycopg

from src.rag.db_roles import APP_TABLES

PRODUCTION = "production"
API_DOC_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


def app_env() -> str:
    return os.environ.get("APP_ENV", "development").strip().lower()


def is_production() -> bool:
    return app_env() == PRODUCTION


# --- registration ------------------------------------------------------------------

def registration_mode() -> str:
    mode = os.environ.get("REGISTRATION_MODE", "closed" if is_production() else "open").strip().lower()
    if mode not in ("open", "closed"):
        raise ValueError("REGISTRATION_MODE must be 'open' or 'closed'")
    return mode


def registration_open() -> bool:
    return registration_mode() == "open"


# --- rate limits -------------------------------------------------------------------

@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int

    def __str__(self) -> str:
        return f"{self.count} per {self.seconds}s"


RATE_LIMIT_DEFAULTS = {
    "LOGIN": "5/minute",
    "REGISTER": "3/hour",
    "QUERIES": "30/minute",
    "ANSWERS": "20/day",
    "UPLOADS": "20/hour",
}
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_limit(text: str) -> Limit | None:
    """'5/minute' -> Limit(5, 60); 'off' -> None."""
    text = text.strip().lower()
    if text in ("off", "none", "unlimited"):
        return None
    match = re.fullmatch(r"(\d+)\s*/\s*(second|minute|hour|day)s?", text)
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"invalid rate limit {text!r}: use '<count>/<second|minute|hour|day>' or 'off'")
    return Limit(int(match.group(1)), _UNITS[match.group(2)])


def rate_limits_enabled() -> bool:
    raw = os.environ.get("RATE_LIMITS_ENABLED")
    if raw is None:
        return is_production()
    if raw.strip().lower() not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError("RATE_LIMITS_ENABLED must be true or false")
    return raw.strip().lower() in ("true", "1", "yes")


def rate_limit(name: str) -> Limit | None:
    """The configured limit for LOGIN/REGISTER/QUERIES/ANSWERS/UPLOADS, or None if disabled."""
    if not rate_limits_enabled():
        return None
    return parse_limit(os.environ.get(f"RATE_LIMIT_{name}", RATE_LIMIT_DEFAULTS[name]))


def validate_limit_settings() -> None:
    """Fail at startup (not on the first request) if any limit setting is malformed."""
    from src.rag.uploads import tenant_max_documents, tenant_max_upload_bytes

    registration_mode()
    rate_limits_enabled()
    for name, default in RATE_LIMIT_DEFAULTS.items():
        parse_limit(os.environ.get(f"RATE_LIMIT_{name}", default))
    tenant_max_documents()
    tenant_max_upload_bytes()


class UnsafeDatabaseRole(RuntimeError):
    pass


def verify_least_privilege(conn: psycopg.Connection) -> None:
    """Raise UnsafeDatabaseRole if the connection's role is a superuser or owns application tables."""
    role, superuser, can_create_roles, can_create_db = conn.execute(
        "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if superuser or can_create_roles or can_create_db:
        raise UnsafeDatabaseRole(f"database role {role!r} is too powerful for the API (superuser/createrole/createdb)")
    owned = [name for (name,) in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user "
        "AND tablename = ANY(%s)", (list(APP_TABLES),))]
    if owned:
        raise UnsafeDatabaseRole(f"database role {role!r} owns {owned}; the API must use the separate app role")


class ProductionConfigError(RuntimeError):
    pass


# Text that shows up in placeholder or copied example secrets. Only words of 6+
# letters: shorter ones (e.g. "dev") occur by chance in real random secrets.
_PLACEHOLDER_WORDS = ("change", "example", "placeholder", "secret", "default", "password", "development", "insecure")


def jwt_secret_problems(key: str) -> list[str]:
    """Why a JWT secret is unfit for production (never includes the secret itself)."""
    problems = []
    if len(key) < 43:  # secrets.token_urlsafe(32) is 43 characters
        problems.append("JWT_SECRET_KEY is shorter than 43 characters")
    if len(set(key)) < 16:
        problems.append("JWT_SECRET_KEY has too little variety to be random")
    lowered = key.lower()
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        problems.append("JWT_SECRET_KEY looks like a placeholder/development value")
    return problems


def validate_production_settings() -> list[str]:
    """Check production-only requirements at startup; raise ProductionConfigError listing every problem.

    Messages name settings but never show their values. Returns non-fatal warnings.
    """
    import tempfile

    from src.rag.db import pool_settings
    from src.rag.uploads import upload_root

    problems = jwt_secret_problems(os.environ.get("JWT_SECRET_KEY", ""))
    if not os.environ.get("APP_DB_USER"):
        problems.append("APP_DB_USER/APP_DB_PASSWORD must be set (python -m src.rag.db_roles create-app-role)")
    try:
        minutes = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
        if not 5 <= minutes <= 1440:
            problems.append("ACCESS_TOKEN_EXPIRE_MINUTES must be between 5 and 1440")
    except ValueError:
        problems.append("ACCESS_TOKEN_EXPIRE_MINUTES must be a whole number")
    try:
        pool_settings()
    except ValueError as error:
        problems.append(str(error))
    try:
        folder = upload_root()
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        problems.append("UPLOAD_DIR is not writable")
    if problems:
        raise ProductionConfigError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))

    warnings = []
    if not os.environ.get("GEMINI_API_KEY"):
        warnings.append("GEMINI_API_KEY is not set: AI answers are disabled (sources-only still works)")
    if os.environ.get("HF_HUB_OFFLINE", "").strip().lower() not in ("1", "true", "yes", "on"):
        warnings.append("HF_HUB_OFFLINE is not set: model loading may contact the Hugging Face Hub")
    return warnings


def check_production_database(connect_app) -> None:
    """Called at startup in production. `connect_app` opens the API's database connection."""
    if not os.environ.get("APP_DB_USER"):
        raise UnsafeDatabaseRole(
            "APP_ENV=production requires APP_DB_USER/APP_DB_PASSWORD (run: python -m src.rag.db_roles create-app-role)"
        )
    with connect_app() as conn:
        verify_least_privilege(conn)
