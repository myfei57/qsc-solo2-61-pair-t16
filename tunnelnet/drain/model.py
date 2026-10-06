"""Sump pits, pump controls and the drain cycles recorded against them."""

from __future__ import annotations

from dataclasses import dataclass

from .timing import utc_text


@dataclass
class Pit:
    """One sump pit and its current water level."""

    id: str
    cabin: str = ""
    water_level: float = 0.0


@dataclass(frozen=True)
class DrainCycle:
    """One completed drain run for a cabin."""

    cabin_id: str
    started_at: str

    def as_dict(self) -> dict[str, str]:
        return {"cabin_id": self.cabin_id, "started_at": self.started_at}


@dataclass(frozen=True)
class SumpConfig:
    """Hysteresis levels and pump protection settings for one sump."""

    start_level: float
    stop_level: float
    min_interval: float = 300.0
    retry_interval: float = 60.0

    def validated(self) -> "SumpConfig":
        if self.start_level <= self.stop_level:
            raise ValueError("start_level must be above stop_level")
        if self.min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        if self.retry_interval < 0:
            raise ValueError("retry_interval must be non-negative")
        return self

    def as_dict(self) -> dict[str, float]:
        return {
            "start_level": self.start_level,
            "stop_level": self.stop_level,
            "min_interval": self.min_interval,
            "retry_interval": self.retry_interval,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SumpConfig":
        return cls(
            start_level=float(payload["start_level"]),
            stop_level=float(payload["stop_level"]),
            min_interval=float(payload.get("min_interval", 300.0)),
            retry_interval=float(payload.get("retry_interval", 60.0)),
        )


@dataclass(frozen=True)
class DrainEvent:
    """Canonical, append-only event used by drain records and audit views."""

    id: int
    pit_id: str
    type: str
    at: float
    level: float = 0.0
    request_id: str = ""
    protect_until: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pit_id": self.pit_id,
            "type": self.type,
            "at": self.at,
            "at_text": utc_text(self.at),
            "level": self.level,
            "request_id": self.request_id,
            "protect_until": self.protect_until,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DrainEvent":
        return cls(
            id=int(payload.get("id", 0)),
            pit_id=str(payload.get("pit_id", "")),
            type=str(payload.get("type", "")),
            at=float(payload.get("at", 0.0)),
            level=float(payload.get("level", 0.0)),
            request_id=str(payload.get("request_id", "")),
            protect_until=float(payload.get("protect_until", 0.0)),
            error=str(payload.get("error", "")),
        )


@dataclass(frozen=True)
class PumpRuntimeState:
    """Durable state required to recover the pump after a process restart."""

    status: str = "idle"
    level: float = 0.0
    protect_until: float = 0.0
    request_id: str = ""
    request_status: str = ""
    last_start: float = 0.0
    last_stop: float = 0.0
    last_error: str = ""
    failure_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "level": self.level,
            "protect_until": self.protect_until,
            "request_id": self.request_id,
            "request_status": self.request_status,
            "last_start": self.last_start,
            "last_stop": self.last_stop,
            "last_error": self.last_error,
            "failure_count": self.failure_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PumpRuntimeState":
        return cls(
            status=str(payload.get("status", "idle")),
            level=float(payload.get("level", 0.0)),
            protect_until=float(payload.get("protect_until", 0.0)),
            request_id=str(payload.get("request_id", "")),
            request_status=str(payload.get("request_status", "")),
            last_start=float(payload.get("last_start", 0.0)),
            last_stop=float(payload.get("last_stop", 0.0)),
            last_error=str(payload.get("last_error", "")),
            failure_count=int(payload.get("failure_count", 0)),
        )
