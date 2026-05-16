"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

import structlog

_SECRET_KEYS = {
    "authorization",
    "api_key",
    "api_key_id",
    "private_key",
    "private_key_path",
    "webhook_url",
    "discord_webhook_url",
    "control_bearer_token",
    "bearer_token",
    "kalshi-access-key",
    "kalshi-access-signature",
}

_WEBHOOK_RE = re.compile(r"https://discord(?:app)?\.com/api/webhooks/[^\s\"']+")


def _redact(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if k.lower() in _SECRET_KEYS else scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        if isinstance(obj, str):
            return _WEBHOOK_RE.sub("https://discord.com/api/webhooks/***REDACTED***", obj)
        return obj

    return scrub(event_dict)


def configure_logging(log_dir: Path | None = None, retention_days: int = 90) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_h = logging.handlers.TimedRotatingFileHandler(
            log_dir / "agent.jsonl",
            when="midnight",
            backupCount=retention_days,
            encoding="utf-8",
        )
        handlers.append(file_h)

    logging.basicConfig(
        format="%(message)s",
        handlers=handlers,
        level=logging.INFO,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
