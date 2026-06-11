"""Structured logging configuration.

Configures structlog with:
- JSON output in production
- Human-readable ConsoleRenderer in development
- Sensitive field filtering (phone numbers, JWT secrets, presigned URLs)
- request_id binding support for middleware
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.config import settings

# ---------------------------------------------------------------------------
# Sensitive-field scrubbing
# ---------------------------------------------------------------------------

_E164_RE = re.compile(r"\+[1-9]\d{6,14}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_PRESIGNED_RE = re.compile(
    r"https?://[^\s]+(?:X-Amz-Signature|x-goog-signature)[^\s]*",
    re.IGNORECASE,
)

_SCRUB_KEYS = frozenset(
    {
        "phone",
        "phone_number",
        "password",
        "jwt_secret",
        "jwt_secret_key",
        "secret_key",
        "access_token",
        "refresh_token",
        "session_token",
        "presigned_url",
        "image_binary",
        "photo_binary",
    }
)


def _scrub_sensitive(
    logger: Any,  # noqa: ANN401
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """Remove or redact sensitive values before the log record is emitted.

    - Keys in ``_SCRUB_KEYS`` are replaced with ``"[REDACTED]"``.
    - String values matching E.164 phone patterns, JWT tokens, or presigned
      URL patterns are redacted inline.
    """
    for key in list(event_dict.keys()):
        if key in _SCRUB_KEYS:
            event_dict[key] = "[REDACTED]"
            continue
        val = event_dict[key]
        if isinstance(val, str):
            val = _E164_RE.sub("[PHONE_REDACTED]", val)
            val = _JWT_RE.sub("[JWT_REDACTED]", val)
            val = _PRESIGNED_RE.sub("[PRESIGNED_REDACTED]", val)
            event_dict[key] = val
    return event_dict


# ---------------------------------------------------------------------------
# structlog configuration
# ---------------------------------------------------------------------------

_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    _scrub_sensitive,
    structlog.processors.StackInfoRenderer(),
]


def configure_logging() -> None:
    """Configure structlog and the stdlib root logger.

    Call once at application startup (inside the lifespan context manager or
    at module import time in ``main.py``).
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    is_production = settings.ENVIRONMENT == "production"

    if is_production:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=_SHARED_PROCESSORS,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    # Silence noisy third-party loggers in production
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(
            logging.WARNING if is_production else log_level
        )
