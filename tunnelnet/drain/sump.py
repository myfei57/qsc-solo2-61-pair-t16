"""File backed sump pump controller with hysteresis and protection.

One :class:`SumpController` owns one pit/pump pair. The pure decision
rules live in :mod:`tunnelnet.drain.protect`; this layer adds:

* a small state machine (stopped / starting / running / protected /
  fault locked),
* durable state and an append only event journal, both restored on
  restart (see :class:`tunnelnet.drain.journal.SumpJournal`),
* idempotent start requests (duplicate request keys return the original
  decision without touching the pump),
* bounded retry with backoff after failed starts, ending in a manual
  reset fault lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from tunnelnet.store import Store

from . import protect
from .journal import (
    EV_FAULT_ALARM,
    EV_PROTECTION,
    EV_RECOVERED,
    EV_RESET,
    EV_START,
    EV_START_BLOCKED,
    EV_START_EMERGENCY,
    EV_START_FAILED,
    EV_STOP,
    EV_STOP_FAILED,
    SumpJournal,
)
from .protect import (
    BLOCKED,
    COOLDOWN,
    FAULT,
    FAULT_BLOCKED,
    HOLD,
    START,
    START_EMERGENCY,
    START_FAILURE,
    STOP,
    Protection,
    SumpConfig,
)
from .retry import utc_now

STATE_KIND = "sump-state"
_REQUEST_CACHE_LIMIT = 64

DEFAULT_CONFIG = SumpConfig(
    start_level=0.80,
    stop_level=0.30,
    emergency_level=1.20,
    min_interval=300.0,
    max_attempts=3,
)


class Pump(Protocol):
    """The pump actuator. Either call may raise to report hardware failure."""

    def start(self) -> None:  # pragma: no cover - structural
        ...

    def stop(self) -> None:  # pragma: no cover - structural
        ...


@dataclass(frozen=True)
class Decision:
    """Result of one control evaluation, safe to hand back to callers."""

    action: str
    pit_id: str
    level: float
    running: bool
    protection_reason: str = ""
    protection_remaining: float = 0.0
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "pit_id": self.pit_id,
            "level": self.level,
            "running": self.running,
            "protection_reason": self.protection_reason,
            "protection_remaining": self.protection_remaining,
            "detail": self.detail,
        }


@dataclass
class _RuntimeState:
    """Everything that must survive a process restart."""

    running: bool = False
    level: float = 0.0
    window: dict[str, object] | None = None
    failed_attempts: int = 0
    run_started_at: str | None = None
    run_start_seq: int | None = None
    run_start_level: float | None = None
    last_action: str = ""
    seen_requests: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "running": self.running,
            "level": self.level,
            "window": self.window,
            "failed_attempts": self.failed_attempts,
            "run_started_at": self.run_started_at,
            "run_start_seq": self.run_start_seq,
            "run_start_level": self.run_start_level,
            "last_action": self.last_action,
            "seen_requests": self.seen_requests,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "_RuntimeState":
        window = payload.get("window")
        seen = payload.get("seen_requests")
        return cls(
            running=bool(payload.get("running", False)),
            level=float(payload.get("level", 0.0)),
            window=dict(window) if isinstance(window, dict) else None,
            failed_attempts=int(payload.get("failed_attempts", 0)),
            run_started_at=_optional_str(payload.get("run_started_at")),
            run_start_seq=_optional_int(payload.get("run_start_seq")),
            run_start_level=_optional_float(payload.get("run_start_level")),
            last_action=str(payload.get("last_action", "")),
            seen_requests=dict(seen) if isinstance(seen, dict) else {},
        )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


class SumpController:
    """Hysteresis pump control with cooldown, emergency override and retry."""

    def __init__(
        self,
        pit_id: str,
        config: SumpConfig,
        pump: Pump,
        store: Store,
        clock: Callable[[], float] | None = None,
        stamp: Callable[[], str] = utc_now,
    ) -> None:
        import time

        self._pit_id = pit_id
        self._config = config
        self._pump = pump
        self._store = store
        self._clock = clock or time.time
        self._stamp = stamp
        self._lock = threading.RLock()
        self._journal = SumpJournal(store, pit_id)
        self._state = self._load()
        self._protection = Protection.from_dict(self._state.window)
        # Edge flags so a blocked high level logs exactly one event, not one
        # per wavy surface sample.
        self._blocked_logged = False
        self._alarm_logged = self._protection.is_fault()
        self._recover_if_restarted()

    # ------------------------------------------------------------------ setup

    def _load(self) -> _RuntimeState:
        payload = self._store.load(STATE_KIND, self._pit_id)
        return _RuntimeState.from_dict(payload) if payload else _RuntimeState()

    def _recover_if_restarted(self) -> None:
        """Reconcile with durable state after a restart.

        A running pump is trusted to still be running (the actuator is
        independent of this process) and an active cooldown/backoff/fault
        window is restored verbatim. A failed start is simply retried once
        its window ends; a pump that physically started but died before the
        state was saved is picked up on the next level sample.
        """

        payload = self._store.load(STATE_KIND, self._pit_id)
        if payload is None:
            return
        self._journal.append(
            EV_RECOVERED,
            self._stamp(),
            level=self._state.level,
            running=self._state.running,
            reason=self._protection.window.reason if self._protection.window else "",
            detail={"last_action": self._state.last_action},
        )
        self._persist()

    def _persist(self) -> None:
        self._state.window = self._protection.as_dict()
        self._store.save(STATE_KIND, self._pit_id, self._state.as_dict())

    # ------------------------------------------------------------- public API

    @property
    def pit_id(self) -> str:
        return self._pit_id

    @property
    def config(self) -> SumpConfig:
        return self._config

    def observe(self, level: float) -> Decision:
        """Feed one level sample and run the automatic control loop."""

        with self._lock:
            self._state.level = level
            return self._evaluate(manual=False, request_id=None)

    def request_start(self, request_id: str | None = None) -> Decision:
        """Operator start request; duplicate ids are idempotent no-ops.

        A manual request creates start demand at any level, but it never
        bypasses a protection window — only the emergency level does.
        """

        with self._lock:
            if request_id is not None and request_id in self._state.seen_requests:
                return self._decision(self._state.seen_requests[request_id])
            decision = self._evaluate(manual=True, request_id=request_id)
            if request_id is not None:
                self._remember_request(request_id, decision.action)
                self._persist()  # the cached decision must survive a restart
            return decision

    def reset_fault(self) -> bool:
        """Clear the fault lock after the pump has been inspected.

        Returns whether a fault was actually cleared; the call itself is
        idempotent.
        """

        with self._lock:
            if not self._protection.is_fault():
                return False
            self._protection = self._protection.clear_fault()
            self._state.failed_attempts = 0
            self._alarm_logged = False
            self._blocked_logged = False
            self._journal.append(EV_RESET, self._stamp(), level=self._state.level, reason=FAULT)
            self._state.last_action = EV_RESET
            self._persist()
            return True

    def running(self) -> bool:
        with self._lock:
            return self._state.running

    def level(self) -> float:
        with self._lock:
            return self._state.level

    def protection(self) -> Protection:
        with self._lock:
            return self._protection.effective(self._clock())

    def journal(self) -> SumpJournal:
        return self._journal

    def snapshot(self) -> dict[str, object]:
        """Current level, pump and protection state for dashboards/backups."""

        with self._lock:
            now = self._clock()
            effective = self._protection.effective(now)
            window = effective.window
            return {
                "pit_id": self._pit_id,
                "level": self._state.level,
                "running": self._state.running,
                "start_level": self._config.start_level,
                "stop_level": self._config.stop_level,
                "emergency_level": self._config.emergency_level,
                "min_interval": self._config.min_interval,
                "protection": None
                if window is None
                else {
                    "reason": window.reason,
                    "remaining": None if window.reason == FAULT else window.remaining(now),
                    "until": window.as_dict()["until"],
                },
                "failed_attempts": self._state.failed_attempts,
                "last_action": self._state.last_action,
                "event_count": len(self._journal),
                "current_run": None
                if self._state.run_started_at is None
                else {
                    "started_at": self._state.run_started_at,
                    "start_seq": self._state.run_start_seq,
                    "start_level": self._state.run_start_level,
                },
            }

    # -------------------------------------------------------------- evaluation

    def _evaluate(self, *, manual: bool, request_id: str | None) -> Decision:
        now = self._clock()
        self._protection = self._protection.effective(now)
        action = protect.decide(
            level=self._state.level,
            running=self._state.running,
            config=self._config,
            protection=self._protection,
            now=now,
            manual=manual,
        )

        if action in (START, START_EMERGENCY):
            action = self._do_start(now, emergency=action == START_EMERGENCY)
        elif action == STOP:
            self._do_stop(now)
        elif action == BLOCKED:
            self._note_blocked(now)
        elif action == FAULT_BLOCKED:
            self._note_fault_alarm()
        else:  # HOLD: water moved away, a future block must be logged anew
            self._blocked_logged = False

        self._state.last_action = action
        self._persist()
        return self._decision(action)

    def _do_start(self, now: float, *, emergency: bool) -> str:
        """Attempt a start; return the action that actually took effect."""

        if emergency:
            # The emergency level is the single override of TIME based
            # windows; the fault lock never reaches this branch.
            self._protection = self._protection.clear_time_windows()
            event_name = EV_START_EMERGENCY
        else:
            event_name = EV_START
        try:
            self._pump.start()
        except Exception as exc:  # actuator rejected the command
            return self._on_start_failure(now, exc)

        self._state.running = True
        self._state.failed_attempts = 0
        self._blocked_logged = False
        self._alarm_logged = False
        event = self._journal.append(
            event_name,
            self._stamp(),
            level=self._state.level,
            running=True,
            reason="emergency" if emergency else "demand",
            detail={"request_protection_cleared": emergency},
        )
        self._state.run_started_at = event.at
        self._state.run_start_seq = event.seq
        self._state.run_start_level = self._state.level
        return START_EMERGENCY if emergency else START

    def _on_start_failure(self, now: float, exc: Exception) -> str:
        """Backoff and retry; escalate to a fault lock after max attempts."""

        self._state.failed_attempts += 1
        attempts = self._state.failed_attempts
        self._journal.append(
            EV_START_FAILED,
            self._stamp(),
            level=self._state.level,
            running=False,
            reason=START_FAILURE,
            detail={"attempt": attempts, "error": str(exc) or exc.__class__.__name__},
        )
        if attempts >= self._config.max_attempts:
            self._protection = self._protection.engage(FAULT, float("inf"))
            self._alarm_logged = False  # force one fresh fault alarm
            self._note_fault_alarm()
            return FAULT_BLOCKED

        until = self._config.backoff(now, attempts)
        self._protection = self._protection.engage(START_FAILURE, until)
        self._journal.append(
            EV_PROTECTION,
            self._stamp(),
            level=self._state.level,
            reason=START_FAILURE,
            detail={"until": until, "attempt": attempts},
        )
        return BLOCKED

    def _do_stop(self, now: float) -> None:
        try:
            self._pump.stop()
        except Exception as exc:
            # State is unknown, so keep assuming "running" and alarm.
            self._journal.append(
                EV_STOP_FAILED,
                self._stamp(),
                level=self._state.level,
                running=True,
                reason="stop_failed",
                detail={"error": str(exc) or exc.__class__.__name__},
            )
            return

        self._state.running = False
        self._journal.append(
            EV_STOP,
            self._stamp(),
            level=self._state.level,
            running=False,
        )
        # Every stop earns the pump its minimum rest interval. Overlapping
        # windows merge by latest end time, never by summing durations.
        until = now + self._config.min_interval
        self._protection = self._protection.effective(now).engage(COOLDOWN, until)
        self._journal.append(
            EV_PROTECTION,
            self._stamp(),
            level=self._state.level,
            reason=COOLDOWN,
            detail={"until": until, "min_interval": self._config.min_interval},
        )
        self._state.run_started_at = None
        self._state.run_start_seq = None
        self._state.run_start_level = None

    def _note_blocked(self, now: float) -> None:
        if self._blocked_logged:
            return
        window = self._protection.window
        self._blocked_logged = True
        self._journal.append(
            EV_START_BLOCKED,
            self._stamp(),
            level=self._state.level,
            reason=window.reason if window else COOLDOWN,
            detail={"remaining": window.remaining(now) if window else 0.0},
        )

    def _note_fault_alarm(self) -> None:
        if self._alarm_logged:
            return
        self._alarm_logged = True
        self._journal.append(
            EV_FAULT_ALARM,
            self._stamp(),
            level=self._state.level,
            running=self._state.running,
            reason=FAULT,
            detail={"failed_attempts": self._state.failed_attempts},
        )

    def _remember_request(self, request_id: str, action: str) -> None:
        seen = self._state.seen_requests
        seen[request_id] = action
        if len(seen) > _REQUEST_CACHE_LIMIT:
            for oldest in list(seen)[:-_REQUEST_CACHE_LIMIT]:
                seen.pop(oldest)

    def _decision(self, action: str) -> Decision:
        now = self._clock()
        effective = self._protection.effective(now)
        window = effective.window
        return Decision(
            action=action,
            pit_id=self._pit_id,
            level=self._state.level,
            running=self._state.running,
            protection_reason=window.reason if window else "",
            protection_remaining=window.remaining(now) if window else 0.0,
        )
