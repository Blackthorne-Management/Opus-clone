"""Logging with a rotating file and a filter that scrubs secrets from every record."""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import SECRET_ENV_VARS, Config

_KEYISH = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{20,}|AIza[0-9A-Za-z_\-]{30,}"
                     r"|(?i:apikey)\s+[A-Za-z0-9._\-]{12,})")


class RedactSecrets(logging.Filter):
    """Replace any configured secret value (and anything key-shaped) with ***."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        scrubbed = redact(msg)
        if scrubbed != msg:
            record.msg, record.args = scrubbed, None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= 6:
            text = text.replace(value, "***")
    return _KEYISH.sub("***", text)


def setup_logging(cfg: Config) -> logging.Logger:
    root = logging.getLogger()
    if getattr(root, "_kir_configured", False):
        return logging.getLogger("kir")
    root.setLevel(cfg.get("logging.level", "INFO"))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redactor = RedactSecrets()

    if sys.stderr is not None:  # None under pythonw.exe (background service)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(redactor)
        root.addHandler(console)

    log_dir = cfg.subdir("logs")
    file_handler = RotatingFileHandler(log_dir / "kir_clipper.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "watchdog", "anthropic", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._kir_configured = True  # type: ignore[attr-defined]
    return logging.getLogger("kir")
