"""Pure pump protection timing calculations."""

from __future__ import annotations


def extended_until(current_until: float, now: float, interval: float) -> float:
    """Return the end of overlapping protection windows.

    Overlapping windows are merged, not queued. A new stop/start extends the
    active window to ``now + interval`` while preserving any later deadline.
    """

    return max(current_until, now + interval)


def is_protected(protect_until: float, now: float) -> bool:
    return protect_until > now


def remaining(protect_until: float, now: float) -> float:
    return max(0.0, protect_until - now)
