"""Protected, restart-safe controller for one sump pump."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from tunnelnet.store import Store

from .events import DrainEventLog, audit_entries
from .model import DrainEvent, PumpRuntimeState, SumpConfig
from .persistence import load_config, load_runtime, save_config, save_runtime
from .protection import extended_until, is_protected, remaining
from .report import ControlResult, DrainRun, SumpSnapshot, runs_from_events
from .timing import monotonic_seconds


class ValveController(Protocol):
    """Opens the inlet valve before the pump starts."""

    def open_valve(self, cabin_id: str) -> None:  # pragma: no cover - structural
        ...


class SumpPumpController(Protocol):
    """Physical pump used by one sump controller."""

    def start(self) -> None:  # pragma: no cover - structural
        ...

    def stop(self) -> None:  # pragma: no cover - structural
        ...

    def is_running(self) -> bool:  # pragma: no cover - structural
        ...


class SumpController:
    """Hysteresis control with a merge-on-overlap pump protection window.

    Policy choice: while protection is active and the water merely remains
    above the normal start level, the pump waits. The controller does not
    override its own protection window; an external emergency bypass must be
    deliberately separate so routine ripples cannot defeat pump protection.
    Overlapping windows use the later expiry, never a queue of extra delays.
    """

    def __init__(
        self,
        pit_id: str,
        config: SumpConfig,
        pump: SumpPumpController,
        store: Store,
        valve: ValveController | None = None,
        cabin_id: str = "",
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        self._pit_id = pit_id
        self._pump = pump
        self._valve = valve
        self._cabin_id = cabin_id
        self._clock = clock
        self._lock = threading.RLock()
        self._events = DrainEventLog(store)
        self._store = store
        self._config = config.validated()
        stored_config = load_config(store, pit_id)
        if stored_config is not None:
            self._config = stored_config
        else:
            save_config(store, pit_id, self._config)
        state = load_runtime(store, pit_id)
        self._state = state if state is not None else PumpRuntimeState()
        self._recover_after_restart()
        save_runtime(store, pit_id, self._state)

    def _now(self) -> float:
        return float(self._clock())

    def _save(self) -> None:
        save_runtime(self._store, self._pit_id, self._state)

    def _append(
        self,
        event_type: str,
        *,
        at: float | None = None,
        level: float | None = None,
        request_id: str = "",
        protect_until: float = 0.0,
        error: str = "",
    ) -> DrainEvent:
        return self._events.append(
            self._pit_id,
            event_type,
            self._now() if at is None else at,
            level=self._state.level if level is None else level,
            request_id=request_id,
            protect_until=protect_until,
            error=error,
        )

    def _recover_after_restart(self) -> None:
        now = self._now()
        if self._state.status == "idle" and not self._state.request_id:
            pending = self._events.latest_pending_request(self._pit_id)
            if pending is not None:
                self._state = replace(
                    self._state,
                    request_id=pending[0],
                    request_status="pending",
                )

        if self._state.status == "starting":
            running = self._physical_running()
            if running:
                self._state = replace(
                    self._state,
                    status="running",
                    request_status="started",
                    last_start=now if self._state.last_start == 0 else self._state.last_start,
                )
                self._append(
                    "recovered",
                    at=now,
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                )
            else:
                protect_until = extended_until(
                    self._state.protect_until, now, self._config.retry_interval
                )
                event = self._append(
                    "failed",
                    at=now,
                    request_id=self._state.request_id,
                    protect_until=protect_until,
                    error="start result unknown at restart",
                )
                self._state = replace(
                    self._state,
                    status="failed",
                    request_status="failed",
                    protect_until=protect_until,
                    last_error=event.error,
                    failure_count=self._state.failure_count + 1,
                )
        elif self._state.status == "running" and not self._physical_running():
            protect_until = extended_until(
                self._state.protect_until, now, self._config.retry_interval
            )
            event = self._append(
                "failed",
                at=now,
                request_id=self._state.request_id,
                protect_until=protect_until,
                error="pump not running at restart",
            )
            self._state = replace(
                self._state,
                status="failed",
                request_status="failed",
                protect_until=protect_until,
                last_error=event.error,
                failure_count=self._state.failure_count + 1,
            )

    def _physical_running(self) -> bool:
        is_running = getattr(self._pump, "is_running", None)
        if not callable(is_running):
            # A legacy fake without feedback cannot safely prove a start.
            return False
        try:
            return bool(is_running())
        except Exception:
            return False

    def _safe_stop_during_failure(self) -> None:
        stop = getattr(self._pump, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def configure(self, config: SumpConfig) -> ControlResult:
        with self._lock:
            self._config = config.validated()
            save_config(self._store, self._pit_id, self._config)
            event = self._append("configured", protect_until=self._state.protect_until)
            return ControlResult(
                action="configured",
                event_id=event.id,
                protect_until=self._state.protect_until,
            )

    def config(self) -> SumpConfig:
        with self._lock:
            return self._config

    def status(self, now: float | None = None) -> SumpSnapshot:
        with self._lock:
            current = self._now() if now is None else now
            return SumpSnapshot(
                pit_id=self._pit_id,
                config=self._config,
                runtime=self._state,
                now=current,
            )

    def events(self) -> list[DrainEvent]:
        with self._lock:
            return self._events.events(self._pit_id)

    def runs(self) -> list[DrainRun]:
        return runs_from_events(self.events())

    def audit_entries(self) -> list[dict[str, object]]:
        return audit_entries(self.events())

    def _known_outcome(self, request_id: str) -> str:
        return self._events.request_outcomes().get(request_id, "")

    def _try_start(self, request_id: str, now: float) -> ControlResult:
        self._append("requested", at=now, request_id=request_id)
        if is_protected(self._state.protect_until, now):
            event = self._append(
                "blocked",
                at=now,
                request_id=request_id,
                protect_until=self._state.protect_until,
            )
            self._state = replace(
                self._state,
                request_id=request_id,
                request_status="pending"
                if self._state.status == "idle"
                else self._state.request_status,
            )
            self._save()
            return ControlResult(
                action="blocked",
                request_id=request_id,
                event_id=event.id,
                protect_until=self._state.protect_until,
            )

        self._state = replace(
            self._state,
            status="starting",
            request_id=request_id,
            request_status="starting",
            last_error="",
        )
        self._save()
        try:
            if self._valve is not None:
                self._valve.open_valve(self._cabin_id or self._pit_id)
            self._pump.start()
        except Exception as exc:
            self._safe_stop_during_failure()
            protect_until = extended_until(
                self._state.protect_until, now, self._config.retry_interval
            )
            event = self._append(
                "failed",
                at=now,
                request_id=request_id,
                protect_until=protect_until,
                error=str(exc),
            )
            self._state = replace(
                self._state,
                status="failed",
                request_status="failed",
                protect_until=protect_until,
                last_error=str(exc),
                failure_count=self._state.failure_count + 1,
            )
            self._save()
            return ControlResult(
                action="failed",
                request_id=request_id,
                event_id=event.id,
                protect_until=protect_until,
                error=str(exc),
            )

        if not self._physical_running():
            self._safe_stop_during_failure()
            protect_until = extended_until(
                self._state.protect_until, now, self._config.retry_interval
            )
            error = "pump did not report running"
            event = self._append(
                "failed",
                at=now,
                request_id=request_id,
                protect_until=protect_until,
                error=error,
            )
            self._state = replace(
                self._state,
                status="failed",
                request_status="failed",
                protect_until=protect_until,
                last_error=error,
                failure_count=self._state.failure_count + 1,
            )
            self._save()
            return ControlResult(
                action="failed",
                request_id=request_id,
                event_id=event.id,
                protect_until=protect_until,
                error=error,
            )

        protect_until = extended_until(
            self._state.protect_until, now, self._config.min_interval
        )
        event = self._append(
            "started",
            at=now,
            request_id=request_id,
            protect_until=protect_until,
        )
        self._state = replace(
            self._state,
            status="running",
            request_status="started",
            protect_until=protect_until,
            last_start=now,
            last_error="",
        )
        self._save()
        return ControlResult(
            action="started",
            request_id=request_id,
            event_id=event.id,
            protect_until=protect_until,
        )

    def request_start(self, request_id: str, now: float | None = None) -> ControlResult:
        with self._lock:
            current = self._now() if now is None else now
            if not request_id:
                raise ValueError("request_id is required")

            if request_id == self._state.request_id:
                if self._state.request_status in {"started", "starting"}:
                    return ControlResult(
                        action=self._state.request_status,
                        request_id=request_id,
                        protect_until=self._state.protect_until,
                    )
                if self._state.request_status == "pending":
                    if is_protected(self._state.protect_until, current):
                        return ControlResult(
                            action="blocked",
                            request_id=request_id,
                            protect_until=self._state.protect_until,
                        )
                    return self._try_start(request_id, current)
                if self._state.request_status == "failed":
                    return self._try_start(request_id, current)

            outcome = self._known_outcome(request_id)
            if outcome in {"started", "failed", "withdrawn"}:
                return ControlResult(
                    action=outcome,
                    request_id=request_id,
                    protect_until=self._state.protect_until,
                )

            if self._state.status == "running":
                return ControlResult(
                    action="already_running",
                    request_id=request_id,
                    protect_until=self._state.protect_until,
                )
            if self._state.status == "starting":
                return ControlResult(
                    action="starting",
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                )

            return self._try_start(request_id, current)

    def sample(self, level: float, now: float | None = None) -> ControlResult:
        with self._lock:
            current = self._now() if now is None else now
            self._state = replace(self._state, level=level)
            self._save()

            if self._state.status == "running":
                if level <= self._config.stop_level:
                    return self._stop(current, manual=False)
                return ControlResult(
                    action="running",
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                )

            if self._state.status == "starting":
                return ControlResult(
                    action="starting",
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                )

            if self._state.status == "failed":
                if level >= self._config.start_level and self._state.request_id:
                    return self.request_start(self._state.request_id, current)
                return ControlResult(
                    action="failed",
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                    error=self._state.last_error,
                )

            if self._state.request_status == "pending":
                if level < self._config.start_level:
                    event = self._append(
                        "withdrawn",
                        at=current,
                        request_id=self._state.request_id,
                        protect_until=self._state.protect_until,
                    )
                    self._state = replace(self._state, request_id="", request_status="")
                    self._save()
                    return ControlResult(
                        action="withdrawn",
                        request_id=event.request_id,
                        event_id=event.id,
                        protect_until=self._state.protect_until,
                    )
                return self.request_start(self._state.request_id, current)

            if level >= self._config.start_level:
                request_id = self._events.next_request_id(self._pit_id)
                return self.request_start(request_id, current)

            return ControlResult(action="idle")

    def tick(self, now: float | None = None) -> list[ControlResult]:
        with self._lock:
            current = self._now() if now is None else now
            results: list[ControlResult] = []
            if (
                self._state.request_status == "pending"
                and self._state.level >= self._config.start_level
            ):
                results.append(self.request_start(self._state.request_id, current))
            if (
                self._state.status == "failed"
                and self._state.level >= self._config.start_level
            ):
                results.append(self.request_start(self._state.request_id, current))
            return results

    def _stop(self, now: float, manual: bool) -> ControlResult:
        if self._state.status not in {"running", "starting"}:
            return ControlResult(
                action="not_running",
                request_id=self._state.request_id,
                protect_until=self._state.protect_until,
            )

        request_id = self._state.request_id
        if self._state.status == "running":
            self._pump.stop()
        protect_until = extended_until(
            self._state.protect_until, now, self._config.min_interval
        )
        event = self._append(
            "stopped",
            at=now,
            request_id=request_id,
            protect_until=protect_until,
        )
        self._state = replace(
            self._state,
            status="idle",
            request_id="",
            request_status="",
            protect_until=protect_until,
            last_stop=now,
            last_error="",
        )
        self._save()
        return ControlResult(
            action="stopped" if not manual else "manual_stopped",
            request_id=request_id,
            event_id=event.id,
            protect_until=protect_until,
        )

    def stop(self, now: float | None = None) -> ControlResult:
        with self._lock:
            return self._stop(self._now() if now is None else now, manual=True)

    def reset_failure(self, now: float | None = None) -> ControlResult:
        with self._lock:
            current = self._now() if now is None else now
            if self._state.status != "failed":
                return ControlResult(
                    action="not_failed",
                    request_id=self._state.request_id,
                    protect_until=self._state.protect_until,
                )
            protect_until = extended_until(
                self._state.protect_until, current, self._config.retry_interval
            )
            event = self._append(
                "reset",
                at=current,
                request_id=self._state.request_id,
                protect_until=protect_until,
            )
            self._state = replace(
                self._state,
                status="idle",
                request_id="",
                request_status="",
                protect_until=protect_until,
                last_error="",
            )
            self._save()
            return ControlResult(
                action="reset",
                request_id=event.request_id,
                event_id=event.id,
                protect_until=protect_until,
            )

    def protection_remaining(self, now: float | None = None) -> float:
        with self._lock:
            current = self._now() if now is None else now
            return remaining(self._state.protect_until, current)
