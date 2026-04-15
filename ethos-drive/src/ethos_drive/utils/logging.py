"""Logging configuration for EthOS Drive."""

import logging
import logging.handlers
import sys
from pathlib import Path

import platformdirs


def setup_logging(level: str = "INFO"):
    """Configure application logging."""
    log_dir = Path(platformdirs.user_log_dir("EthOS Drive", "EthOS"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ethos-drive.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-25s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — 5 MB per file, keep 5 backups
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler (stderr)
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    ch.setLevel(logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING)
    root.addHandler(ch)

    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)

    logging.getLogger(__name__).info("Logging initialized — file: %s", log_file)
