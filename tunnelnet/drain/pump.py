"""The pump actuator driven by the drain service."""

from __future__ import annotations


class PumpUnit:
    """In-memory pump used by tests and the demo console."""

    def __init__(self) -> None:
        self._runs = 0
        self._stops = 0
        self._running = False

    def start(self) -> None:
        self._runs += 1
        self._running = True

    def stop(self) -> None:
        self._stops += 1
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def runs(self) -> int:
        return self._runs

    def stops(self) -> int:
        return self._stops

    def describe(self) -> str:
        return f"pump runs={self._runs} stops={self._stops} running={self._running}"
