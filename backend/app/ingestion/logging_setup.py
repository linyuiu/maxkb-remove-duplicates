"""Rotating JSON operational logs; task audit trails remain queryable in PostgreSQL."""

import json
import logging
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

request_id = ContextVar("request_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "worker": record.threadName,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in (
            "file_id",
            "batch_id",
            "attempt_id",
            "request_id",
            "source_job_id",
            "stage",
            "method",
            "path",
            "http_status",
            "elapsed_ms",
        ):
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        if not payload.get("request_id") and request_id.get():
            payload["request_id"] = request_id.get()
        if record.exc_info:
            # Keep the code location without serializing exception values, locals,
            # source lines or request bodies, which may contain credentials/content.
            payload["error_type"] = record.exc_info[0].__name__
            if isinstance(getattr(record.exc_info[1], "errno", None), int):
                payload["errno"] = record.exc_info[1].errno
            payload["traceback"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])[-32:]
            ]
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(root: Path):
    folder = root / "logs"
    folder.mkdir(exist_ok=True)
    logger = logging.getLogger("app.ingestion")
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        folder / "service.jsonl", maxBytes=10 * 1024**2, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # A second root handler could print the unredacted exception value.
    logger.propagate = False
