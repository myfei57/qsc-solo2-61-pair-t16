"""Manage one durable protected controller per sump."""

from __future__ import annotations

import threading
from collections.abc import Callable

from tunnelnet.store import Store

from .controller import SumpController, SumpPumpController, ValveController
from .events import DrainEventLog, audit_entries
from .model import DrainEvent, SumpConfig
from .report import ControlResult, DrainRun, SumpSnapshot, runs_from_events
from .timing import monotonic_seconds

DEFAULT_SUMP_CONFIG = SumpConfig(
    start_level=0.8,
    stop_level=0.3,
    min_interval=300.0,
    retry_interval=60.0,
)

PumpFactory = Callable[[str], SumpPumpController]
ValveFactory = Callable[[str, str], ValveController]


class SumpControlService:
    """Factory and in-memory registry for durable per-sump controllers."""

    def __init__(
        self,
        store: Store,
        pump_factory: PumpFactory,
        valve_factory: ValveFactory | None = None,
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        self._store = store
        self._pump_factory = pump_factory
        self._valve_factory = valve_factory
        self._clock = clock
        self._log = DrainEventLog(store)
        self._controllers: dict[str, SumpController] = {}
        self._cabins: dict[str, str] = {}
        self._lock = threading.RLock()

    def configure(
        self,
        pit_id: str,
        config: SumpConfig = DEFAULT_SUMP_CONFIG,
        cabin_id: str = "",
    ) -> SumpController:
        with self._lock:
            existing = self._controllers.get(pit_id)
            if existing is not None:
                existing.configure(config)
                return existing
            pump = self._pump_factory(pit_id)
            valve = self._valve_factory(pit_id, cabin_id) if self._valve_factory else None
            controller = SumpController(
                pit_id,
                config,
                pump,
                self._store,
                valve=valve,
                cabin_id=cabin_id,
                clock=self._clock,
            )
            self._controllers[pit_id] = controller
            self._cabins[pit_id] = cabin_id
            return controller

    def require(self, pit_id: str) -> SumpController:
        try:
            return self._controllers[pit_id]
        except KeyError:
            raise KeyError(f"sump is not configured: {pit_id}") from None

    def sample(self, pit_id: str, level: float, now: float | None = None) -> ControlResult:
        with self._lock:
            return self.require(pit_id).sample(level, now)

    def request_start(
        self,
        pit_id: str,
        request_id: str,
        now: float | None = None,
    ) -> ControlResult:
        with self._lock:
            return self.require(pit_id).request_start(request_id, now)

    def stop(self, pit_id: str, now: float | None = None) -> ControlResult:
        with self._lock:
            return self.require(pit_id).stop(now)

    def reset_failure(self, pit_id: str, now: float | None = None) -> ControlResult:
        with self._lock:
            return self.require(pit_id).reset_failure(now)

    def tick(self, now: float | None = None) -> list[ControlResult]:
        with self._lock:
            results: list[ControlResult] = []
            for controller in self._controllers.values():
                results.extend(controller.tick(now))
            return results

    def status(self, pit_id: str, now: float | None = None) -> SumpSnapshot:
        with self._lock:
            return self.require(pit_id).status(now)

    def statuses(self, now: float | None = None) -> list[SumpSnapshot]:
        with self._lock:
            return [controller.status(now) for controller in self._controllers.values()]

    def events(self, pit_id: str | None = None) -> list[DrainEvent]:
        return self._log.events(pit_id)

    def runs(self, pit_id: str | None = None) -> list[DrainRun]:
        return runs_from_events(self._log.events(), pit_id)

    def audit_entries(self, pit_id: str | None = None) -> list[dict[str, object]]:
        return audit_entries(self._log.events(pit_id))

    def pit_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._controllers)
