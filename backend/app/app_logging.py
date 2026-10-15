"""Structured JSON logs for Cloud Run (picked up by Cloud Logging with proper severity).

Adapted from dataplex_glossary_approval/src/common/logging.py; adds request_id/user from contextvars.
Set SF_LOG_FORMAT=text for human-readable local logs.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
user_var: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_var.get(),
            "user": user_var.get(),
            "logging.googleapis.com/sourceLocation": {"file": record.pathname, "line": record.lineno, "function": record.funcName},
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = getattr(record, "fields", None)
        return f"{base} {fields}" if fields else base


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else _TextFormatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    root = logging.getLogger("dahub")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    for noisy in ("google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
