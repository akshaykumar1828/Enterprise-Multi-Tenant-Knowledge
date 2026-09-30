"""PostgreSQL-backed fixed-window rate limiting (no Redis).

Each check is one atomic statement: it creates or increments the counter for
(bucket, current window) and returns the new count, so concurrent requests can
never under-count. Once the count exceeds the limit the request is rejected
with 429 and Retry-After = seconds until the window ends.

Buckets:
    login:<ip>:<sha256(email) prefix>   (the email itself is not stored)
    register:<ip>
    query:tenant:<id>   answer:tenant:<id>   upload:tenant:<id>
"""

import hashlib
import math
import random

from src.rag.db import connect_app

from .settings import Limit, rate_limit


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int, message: str, code: str = "rate_limited"):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.message = message
        self.code = code


_HIT_SQL = """
    WITH w AS (
        SELECT to_timestamp(floor(extract(epoch FROM clock_timestamp()) / %(seconds)s) * %(seconds)s) AS start
    )
    INSERT INTO rate_limits (bucket, window_start, expires_at, hits)
    SELECT %(bucket)s, w.start, w.start + make_interval(secs => %(seconds)s), 1 FROM w
    ON CONFLICT (bucket, window_start) DO UPDATE SET hits = rate_limits.hits + 1
    RETURNING hits, extract(epoch FROM expires_at - clock_timestamp())
"""


def hit(bucket: str, limit: Limit) -> tuple[int, int]:
    """Count one request; return (hits in this window, seconds until the window ends)."""
    with connect_app() as conn:
        hits, remaining = conn.execute(_HIT_SQL, {"bucket": bucket, "seconds": limit.seconds}).fetchone()
        if random.random() < 0.01:  # occasional cleanup of finished windows
            conn.execute("DELETE FROM rate_limits WHERE expires_at < now() - interval '1 hour'")
    return hits, max(1, math.ceil(float(remaining)))


def enforce(name: str, bucket: str, message: str, code: str = "rate_limited") -> None:
    """Apply the named limit (see settings.RATE_LIMIT_DEFAULTS) to `bucket`; raise RateLimited when exceeded."""
    limit = rate_limit(name)
    if limit is None:
        return
    hits, retry_after = hit(bucket, limit)
    if hits > limit.count:
        raise RateLimited(retry_after, message, code)


def client_ip(request) -> str:
    # With uvicorn --proxy-headers --forwarded-allow-ips 127.0.0.1 behind Caddy this
    # is the real client address; X-Forwarded-For from anyone else is ignored.
    return request.client.host if request.client else "unknown"


def email_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]
