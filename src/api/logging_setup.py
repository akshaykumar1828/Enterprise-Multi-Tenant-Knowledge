"""Application logging: request IDs, JSON lines in production, and redaction.

  LOG_LEVEL     default INFO
  LOG_FORMAT    json | text      default: json in production, text otherwise
  LOG_REQUESTS  true | false     one line per request; default: true in production

Every log line carries the current request ID. Request lines contain only the
method, path (never the query string), status, duration and client IP; no
headers, bodies, tokens or document text. As a safety net, formatted messages
and tracebacks pass through `redact()`, which masks bearer tokens, JWTs, Google
API keys and "password=..."-style values.
"""

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_REDACTIONS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access_token)(\s*[=:]\s*)[^\s,;&]+"), r"\1\2[REDACTED]"),
]
# Fields a request log line may carry (passed with `extra=`).
REQUEST_FIELDS = ("method", "path", "status", "duration_ms", "client_ip")
_HANDLER_MARK = "_rag_app_handler"
QUIET_LIBRARIES = ("httpx", "httpcore", "urllib3", "huggingface_hub", "sentence_transformers", "transformers",
                   "google_genai", "psycopg.pool")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def request_id_from(header_value: str | None) -> str:
    """Reuse a caller's X-Request-ID only if it is short and harmless; otherwise make one."""
    if header_value and _SAFE_REQUEST_ID.fullmatch(header_value):
        return header_value
    return uuid.uuid4().hex


def _production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() == "production"


def log_requests_enabled() -> bool:
    raw = os.environ.get("LOG_REQUESTS")
    return _production() if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "message": redact(record.getMessage()),
        }
        for name in REQUEST_FIELDS:
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        extras = " ".join(f"{name}={getattr(record, name)}" for name in REQUEST_FIELDS if hasattr(record, name))
        text = super().format(record)
        return redact(f"{text} {extras}" if extras else text)


def configure_logging() -> None:
    """Install one root handler (idempotent: safe to call on every startup)."""
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    fmt = os.environ.get("LOG_FORMAT", "json" if _production() else "text").strip().lower()
    if fmt not in ("json", "text"):
        raise ValueError("LOG_FORMAT must be json or text")

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(level)
    # Third-party libraries: warnings only. httpx in particular logs every outbound
    # request URL at INFO (Hugging Face, and the Gemini client), which must not be logged.
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    if _production():
        # Route uvicorn's own messages through the same (JSON) handler.
        for name in ("uvicorn", "uvicorn.error"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers.clear()
            uvicorn_logger.propagate = True
        # uvicorn's access log stays off (run_api.ps1 --no-access-log): the app writes
        # its own request line with the request id and without the query string.
        access = logging.getLogger("uvicorn.access")
        access.handlers.clear()
        access.propagate = False
