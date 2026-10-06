"""Append-only, durable event log for sump pump controls."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from tunnelnet.store import Store

from .model import DrainEvent

EVENT_KIND = "drain_event"
CONFIG_KIND = "sump_config"
STATE_KIND = "sump_state"


class DrainEventLog:
    """Store canonical control events and replay them in append order."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._sequence = self._load_sequence()

    def _record_id(self, event_id: int) -> str:
        return f"{event_id:012d}"

    def _load_sequence(self) -> int:
        ids = self._store.list(EVENT_KIND)
        return int(ids[-1]) if ids else 0

    def append(
        self,
        pit_id: str,
        event_type: str,
        at: float,
        *,
        level: float = 0.0,
        request_id: str = "",
        protect_until: float = 0.0,
        error: str = "",
    ) -> DrainEvent:
        with self._lock:
            ids = self._store.list(EVENT_KIND)
            self._sequence = int(ids[-1]) if ids else 0
            self._sequence += 1
            event = DrainEvent(
                id=self._sequence,
                pit_id=pit_id,
                type=event_type,
                at=at,
                level=level,
                request_id=request_id,
                protect_until=protect_until,
                error=error,
            )
            self._store.save(EVENT_KIND, self._record_id(event.id), event.as_dict())
            return event

    def events(self, pit_id: str | None = None) -> list[DrainEvent]:
        with self._lock:
            events: list[DrainEvent] = []
            for record_id in self._store.list(EVENT_KIND):
                payload = self._store.load(EVENT_KIND, record_id)
                if payload is None:
                    continue
                event = DrainEvent.from_dict(payload)
                if pit_id is None or event.pit_id == pit_id:
                    events.append(event)
            return sorted(events, key=lambda event: event.id)

    def request_outcomes(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for event in self.events():
            if event.request_id and event.type in {
                "requested",
                "started",
                "failed",
                "withdrawn",
            }:
                outcomes[event.request_id] = event.type
        return outcomes

    def latest_pending_request(self, pit_id: str) -> tuple[str, float] | None:
        """Return the newest request for a pit that has no terminal event."""

        requests: dict[str, tuple[str, float]] = {}
        terminal: set[str] = set()
        for event in self.events(pit_id):
            if not event.request_id:
                continue
            if event.type == "requested":
                requests[event.request_id] = (event.request_id, event.at)
            elif event.type in {"started", "failed", "withdrawn"}:
                terminal.add(event.request_id)
        pending = [item for key, item in requests.items() if key not in terminal]
        return max(pending, key=lambda item: item[1], default=None)

    def request_exists(self, request_id: str) -> bool:
        return bool(request_id) and any(
            event.request_id == request_id for event in self.events()
        )

    def next_request_id(self, pit_id: str) -> str:
        with self._lock:
            count = sum(1 for event in self.events(pit_id) if event.type == "requested")
            return f"auto:{pit_id}:{count + 1}"

    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence


def audit_entries(events: Iterable[DrainEvent]) -> list[dict[str, object]]:
    """Project drain events into audit-shaped records without duplicating data."""

    return [
        {
            "id": f"drain-{event.id:012d}",
            "event": f"drain_{event.type}",
            "at": event.as_dict()["at_text"],
            "pit_id": event.pit_id,
            "request_id": event.request_id,
            "level": event.level,
            "protect_until": event.protect_until,
            "error": event.error,
        }
        for event in sorted(events, key=lambda item: item.id)
    ]
