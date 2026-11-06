import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "operation",
            "decision",
            "reason_code",
            "inference_time_ms",
            "request_id",
            "route",
            "method",
            "status",
            "duration_ms",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    debug: bool = False, log_file: Path | None = None, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5
) -> None:
    formatter = JsonFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        rotating.setFormatter(formatter)
        handlers.append(rotating)
    root = logging.getLogger()
    for old in root.handlers:
        old.close()
    root.handlers = handlers
    root.setLevel(logging.DEBUG if debug else logging.INFO)
