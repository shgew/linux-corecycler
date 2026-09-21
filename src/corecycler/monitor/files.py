"""Shared file-reading boundaries for optional telemetry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


def read_text_optional(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        log.debug("Unable to read optional telemetry file %s", path, exc_info=True)
        return None


def read_int_optional(path: Path) -> int | None:
    text = read_text_optional(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        log.debug("Invalid integer in optional telemetry file %s", path)
        return None
