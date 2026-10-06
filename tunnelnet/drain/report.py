"""Read only projections of the drain service."""

from __future__ import annotations

from dataclasses import dataclass

from .model import DrainCycle, DrainEvent, Pit, PumpRuntimeState, SumpConfig
from .timing import utc_text


@dataclass(frozen=True)
class PitState:
    """An immutable copy of one pit."""

    id: str
    cabin: str
    water_level: float

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "cabin": self.cabin, "water_level": self.water_level}


@dataclass(frozen=True)
class DrainState:
    """Pits, cycles and the level threshold that decides a high pit."""

    pits: list[PitState]
    cycles: list[DrainCycle]
    threshold: float
    runs: int

    def as_dict(self) -> dict[str, object]:
        return {
            "pits": [pit.as_dict() for pit in self.pits],
            "cycles": [cycle.as_dict() for cycle in self.cycles],
            "threshold": self.threshold,
            "runs": self.runs,
        }


@dataclass(frozen=True)
class ControlResult:
    """Outcome returned by one durable control operation."""

    action: str
    request_id: str = ""
    event_id: int = 0
    protect_until: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "request_id": self.request_id,
            "event_id": self.event_id,
            "protect_until": self.protect_until,
            "error": self.error,
        }


@dataclass(frozen=True)
class SumpSnapshot:
    """Read-only view of one protected sump pump controller."""

    pit_id: str
    config: SumpConfig
    runtime: PumpRuntimeState
    now: float

    @property
    def protected(self) -> bool:
        return self.runtime.protect_until > self.now

    @property
    def protection_remaining(self) -> float:
        return max(0.0, self.runtime.protect_until - self.now)

    @property
    def running(self) -> bool:
        return self.runtime.status == "running"

    @property
    def failed(self) -> bool:
        return self.runtime.status == "failed"

    @property
    def pending(self) -> bool:
        return self.runtime.status == "idle" and self.runtime.request_status == "pending"

    def as_dict(self) -> dict[str, object]:
        return {
            "pit_id": self.pit_id,
            "config": self.config.as_dict(),
            "status": self.runtime.status,
            "request_id": self.runtime.request_id,
            "request_status": self.runtime.request_status,
            "level": self.runtime.level,
            "protected": self.protected,
            "protection_until": self.runtime.protect_until,
            "protection_until_text": utc_text(self.runtime.protect_until)
            if self.runtime.protect_until
            else "",
            "protection_remaining": self.protection_remaining,
            "last_start": self.runtime.last_start,
            "last_stop": self.runtime.last_stop,
            "last_error": self.runtime.last_error,
            "failure_count": self.runtime.failure_count,
            "running": self.running,
            "failed": self.failed,
            "pending": self.pending,
        }


@dataclass(frozen=True)
class DrainRun:
    """A completed pump run derived from paired canonical start/stop events."""

    pit_id: str
    request_id: str
    start_event_id: int
    stop_event_id: int
    started_at: float
    stopped_at: float
    start_level: float
    stop_level: float

    def as_dict(self) -> dict[str, object]:
        return {
            "pit_id": self.pit_id,
            "request_id": self.request_id,
            "start_event_id": self.start_event_id,
            "stop_event_id": self.stop_event_id,
            "started_at": self.started_at,
            "started_at_text": utc_text(self.started_at),
            "stopped_at": self.stopped_at,
            "stopped_at_text": utc_text(self.stopped_at),
            "duration": self.stopped_at - self.started_at,
            "start_level": self.start_level,
            "stop_level": self.stop_level,
        }


def pit_state(pit: Pit) -> PitState:
    return PitState(id=pit.id, cabin=pit.cabin, water_level=pit.water_level)


def runs_from_events(events: list[DrainEvent], pit_id: str | None = None) -> list[DrainRun]:
    """Build drainage records from the same events read by audit projections."""

    runs: list[DrainRun] = []
    starts: dict[str, DrainEvent] = {}
    for event in sorted(events, key=lambda item: item.id):
        if pit_id is not None and event.pit_id != pit_id:
            continue
        if event.type == "started":
            starts[event.request_id] = event
        elif event.type == "stopped" and event.request_id in starts:
            start = starts.pop(event.request_id)
            runs.append(
                DrainRun(
                    pit_id=event.pit_id,
                    request_id=event.request_id,
                    start_event_id=start.id,
                    stop_event_id=event.id,
                    started_at=start.at,
                    stopped_at=event.at,
                    start_level=start.level,
                    stop_level=event.level,
                )
            )
    return runs
