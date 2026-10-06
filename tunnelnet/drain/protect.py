"""Hysteresis and time based protection policy for one sump pump.

This module is deliberately pure: no threads, no I/O and an injectable
clock, so every protection rule can be tested deterministically.

Policy decisions (see ``POLICY.md`` for the full rationale):

* Two separate trip levels avoid surface chatter: the pump starts at the
  ``start_level`` and only stops again at the lower ``stop_level``.
* After a stop the pump is held in a cooldown window for ``min_interval``
  seconds; a normal start request waits for that window to end.
* A separate ``emergency_level`` is the flood defence override: while the
  water reaches it, a *time based* window (cooldown or start failure
  backoff) is bypassed. A fault lock is never bypassed by a level, because
  forcing a known broken pump protects neither the pump nor the pit.
* Overlapping windows merge into one window ending at the latest expiry
  (a union, never the sum of the durations).
"""

from __future__ import annotations

from dataclasses import dataclass

# Window reasons.
COOLDOWN = "cooldown"
"""Minimum rest interval after a normal pump stop."""

START_FAILURE = "start_failure"
"""Backoff after a start command failed at the actuator."""

FAULT = "fault"
"""Too many failed starts; only a manual reset clears this."""

TIME_WINDOWS = (COOLDOWN, START_FAILURE)

# Actions returned by :func:`decide`.
START = "start"
"""Start now, no protection is in the way."""

START_EMERGENCY = "start_emergency"
"""Start now and clear the time based window, the emergency level is reached."""

STOP = "stop"
"""The stop level is reached; stopping is never protected."""

BLOCKED = "blocked"
"""Start level reached but a time based window is still active."""

FAULT_BLOCKED = "fault_blocked"
"""Water is high but the pump is fault locked; raise an alarm, do not start."""

HOLD = "hold"
"""Nothing to do."""


@dataclass(frozen=True)
class SumpConfig:
    """Trip levels (same unit as the level samples) and protection timings."""

    start_level: float
    stop_level: float
    emergency_level: float
    min_interval: float
    max_attempts: int = 3
    backoff_factor: float = 2.0
    backoff_cap: float = 1800.0

    def __post_init__(self) -> None:
        if not (self.stop_level < self.start_level < self.emergency_level):
            raise ValueError(
                "levels must satisfy stop_level < start_level < emergency_level"
            )
        if self.min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be at least 1")
        if self.backoff_cap < 0:
            raise ValueError("backoff_cap must be non-negative")

    def backoff(self, now: float, failed_attempts: int) -> float:
        """End time of the backoff window after ``failed_attempts`` failures."""

        raw = self.min_interval * self.backoff_factor ** (failed_attempts - 1)
        return now + min(raw, self.backoff_cap)


@dataclass(frozen=True)
class Window:
    """One active protection window.

    ``until`` is an epoch second. The fault lock uses ``until`` of
    ``float("inf")`` and is only removed manually.
    """

    reason: str
    until: float

    def active(self, now: float) -> bool:
        return now < self.until

    def remaining(self, now: float) -> float:
        return max(0.0, self.until - now)

    def as_dict(self) -> dict[str, object]:
        return {"reason": self.reason, "until": None if self.until == float("inf") else self.until}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Window":
        until = payload.get("until")
        return cls(reason=str(payload.get("reason", COOLDOWN)), until=float("inf") if until is None else float(until))


class Protection:
    """At most one effective window per pump.

    Time based windows merge by taking the latest end time; the fault lock
    is sticky and survives any later time based window.
    """

    def __init__(self, window: Window | None = None) -> None:
        self._window = window

    @property
    def window(self) -> Window | None:
        return self._window

    def is_fault(self) -> bool:
        return self._window is not None and self._window.reason == FAULT

    def active(self, now: float) -> bool:
        return self._window is not None and self._window.active(now)

    def effective(self, now: float) -> "Protection":
        """Drop an expired time based window; the fault lock never expires."""

        window = self._window
        if window is not None and window.reason in TIME_WINDOWS and not window.active(now):
            return Protection()
        return self

    def blocks_start(self, now: float, emergency: bool = False) -> bool:
        """Whether a start is forbidden right now.

        The fault lock always blocks; a time based window blocks unless the
        emergency level provides the flood defence override.
        """

        window = self._window
        if window is None or not window.active(now):
            return False
        if window.reason == FAULT:
            return True
        return not emergency

    def engage(self, reason: str, until: float) -> "Protection":
        """Merge ``reason`` into the current protection.

        Overlapping windows take the latest end time (union); they are never
        summed. A fault lock, present or incoming, wins over everything.
        """

        if reason == FAULT or self.is_fault():
            return Protection(Window(FAULT, float("inf")))
        current = self._window
        if current is None:
            return Protection(Window(reason, until))
        if current.until > until:
            return Protection(current)
        # On an equal end time the incoming (typically stronger) reason wins.
        return Protection(Window(reason, until))

    def clear_time_windows(self) -> "Protection":
        """Remove cooldown/backoff after an emergency override; keep faults."""

        if self.is_fault():
            return self
        return Protection()

    def clear_fault(self) -> "Protection":
        return Protection()

    def as_dict(self) -> dict[str, object] | None:
        return None if self._window is None else self._window.as_dict()

    @classmethod
    def from_dict(cls, payload: object) -> "Protection":
        if not isinstance(payload, dict):
            return cls()
        return cls(Window.from_dict(payload))


def decide(
    *,
    level: float,
    running: bool,
    config: SumpConfig,
    protection: Protection,
    now: float,
    manual: bool = False,
) -> str:
    """Return the action for one control evaluation.

    ``manual`` marks an operator start request; it creates start demand at
    any level but never bypasses a window (only the emergency level does).
    """

    protection = protection.effective(now)

    if running:
        return STOP if level <= config.stop_level else HOLD

    emergency = level >= config.emergency_level
    want_start = manual or level >= config.start_level

    if not want_start:
        return HOLD
    if protection.is_fault():
        return FAULT_BLOCKED
    if emergency:
        return START_EMERGENCY if protection.active(now) else START
    if protection.active(now):
        return BLOCKED
    return START
