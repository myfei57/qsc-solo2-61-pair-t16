"""Time helpers used by durable sump pump controls."""

from __future__ import annotations

import time
from collections.abc import Callable


def monotonic_seconds() -> float:
    """Wall-clock seconds since the epoch; production control timestamps."""

    return time.time()


def utc_text(seconds: float) -> str:
    """Render an epoch second timestamp using the project's UTC text format."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
