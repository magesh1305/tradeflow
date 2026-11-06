"""Structured JSON logging (stdlib only).

Usage: `log.info("order placed", extra={"client_order_id": cid, "strategy": sid})`.
Anything passed via `extra` becomes a top-level JSON field.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any

from app.core.clock import IST

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party loggers that are noisy at INFO.
    for noisy in ("kiteconnect", "urllib3", "apscheduler.executors.default", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
