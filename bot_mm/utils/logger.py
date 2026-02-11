"""Structured logging for BotMM market making bot."""

import logging
import os
import sys
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "botmm.log")

# ANSI colors for console
_COLORS = {
    "RESET": "\033[0m",
    "GREEN": "\033[32m",
    "RED": "\033[31m",
    "YELLOW": "\033[33m",
    "CYAN": "\033[36m",
    "GRAY": "\033[90m",
}


class _ColorFormatter(logging.Formatter):
    """Console formatter with color based on message content and level."""

    _LEVEL_COLORS = {
        logging.WARNING: _COLORS["YELLOW"],
        logging.ERROR: _COLORS["RED"],
        logging.CRITICAL: _COLORS["RED"],
    }

    # Keywords that trigger specific colors
    _MSG_COLORS = {
        "fill": _COLORS["GREEN"],
        "filled": _COLORS["GREEN"],
        "bought": _COLORS["GREEN"],
        "sold": _COLORS["GREEN"],
        "risk": _COLORS["RED"],
        "breach": _COLORS["RED"],
        "liquidat": _COLORS["RED"],
        "circuit": _COLORS["RED"],
        "warning": _COLORS["YELLOW"],
        "skew": _COLORS["YELLOW"],
        "cancel": _COLORS["YELLOW"],
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        base = f"[{ts}] [{record.levelname:<7}] [{record.name}] {record.getMessage()}"

        # Pick color: level-based first, then keyword-based
        color = self._LEVEL_COLORS.get(record.levelno, "")
        if not color:
            msg_lower = record.getMessage().lower()
            for kw, c in self._MSG_COLORS.items():
                if kw in msg_lower:
                    color = c
                    break

        if color:
            return f"{color}{base}{_COLORS['RESET']}"
        return base


class _FileFormatter(logging.Formatter):
    """Plain text formatter for log files."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        return f"[{ts}] [{record.levelname:<7}] [{record.name}] {record.getMessage()}"


def setup_logger(name: str = "botmm", level: int = logging.INFO) -> logging.Logger:
    """Create a logger with console (colored) + file handlers.

    Args:
        name: Logger name (e.g. 'quoter', 'risk', 'inventory').
        level: Logging level.

    Returns:
        Configured stdlib logger.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(_ColorFormatter())
    logger.addHandler(ch)

    # File handler
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(_FileFormatter())
    logger.addHandler(fh)

    return logger
