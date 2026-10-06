"""Single append only journal behind both drainage records and audit views.

Every state transition of the sump controller is one :class:`SumpEvent`
in one ordered log. Drainage runs and audit entries are *projections* of
that same log (see ``drainage.py``), so a run reported to operations can
never disagree with what the audit trail references.

Durability: each append is flushed and fsynced before the event becomes
visible, and the file is read back in order on restart.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from tunnelnet.store import Store

JOURNAL_KIND = "sump-journal"
JOURNAL_ID = "events"

# Event names used by the controller. Audit views surface these verbatim.
EV_LEVEL = "level"
EV_START = "pump_start"
EV_START_EMERGENCY = "pump_start_emergency"
EV_STOP = "pump_stop"
EV_START_BLOCKED = "start_blocked"
EV_FAULT_ALARM = "fault_alarm"
EV_START_FAILED = "start_failed"
EV_STOP_FAILED = "stop_failed"
EV_PROTECTION = "protection"
EV_RESET = "fault_reset"
EV_RECOVERED = "state_recovered"

_TRANSITIONS = {
    EV_START,
    EV_START_EMERGENCY,
    EV_STOP,
    EV_START_BLOCKED,
    EV_FAULT_ALARM,
    EV_START_FAILED,
    EV_PROTECTION,
    EV_RESET,
    EV_RECOVERED,
}


@dataclass(frozen=True)
class SumpEvent:
    """One durable entry in the sump event log."""

    seq: int
    at: str
    event: str
    pit_id: str
    level: float | None = None
    running: bool = False
    reason: str = ""
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "event": self.event,
            "pit_id": self.pit_id,
            "level": self.level,
            "running": self.running,
            "reason": self.reason,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SumpEvent":
        level = payload.get("level")
        detail = payload.get("detail")
        return cls(
            seq=int(payload.get("seq", 0)),
            at=str(payload.get("at", "")),
            event=str(payload.get("event", "")),
            pit_id=str(payload.get("pit_id", "")),
            level=None if level is None else float(level),
            running=bool(payload.get("running", False)),
            reason=str(payload.get("reason", "")),
            detail=dict(detail) if isinstance(detail, dict) else {},
        )


class SumpJournal:
    """One JSON document per pit, an ordered list of events.

    Writes go through the atomic :class:`Store` replacement and are also
    fsynced, so a crash cannot lose a transition that the caller already
    observed.
    """

    def __init__(self, store: Store, pit_id: str) -> None:
        self._store = store
        self._pit_id = pit_id
        self._lock = threading.RLock()
        self._events: list[SumpEvent] = self._load()

    @property
    def pit_id(self) -> str:
        return self._pit_id

    def _record_id(self) -> str:
        return f"{JOURNAL_ID}-{self._pit_id}"

    def _load(self) -> list[SumpEvent]:
        payload = self._store.load(JOURNAL_KIND, self._record_id())
        if not payload:
            return []
        raw = payload.get("events")
        if not isinstance(raw, list):
            return []
        events = [SumpEvent.from_dict(item) for item in raw if isinstance(item, dict)]
        events.sort(key=lambda item: item.seq)
        return events

    def append(
        self,
        event: str,
        at: str,
        *,
        level: float | None = None,
        running: bool = False,
        reason: str = "",
        detail: dict[str, object] | None = None,
    ) -> SumpEvent:
        """Persist one event and return it; safe to call concurrently."""

        with self._lock:
            seq = self._events[-1].seq + 1 if self._events else 1
            entry = SumpEvent(
                seq=seq,
                at=at,
                event=event,
                pit_id=self._pit_id,
                level=level,
                running=running,
                reason=reason,
                detail=dict(detail or {}),
            )
            self._events.append(entry)
            self._flush()
            return entry

    def _flush(self) -> None:
        document = {"pit_id": self._pit_id, "events": [event.as_dict() for event in self._events]}
        self._store.save(JOURNAL_KIND, self._record_id(), document)
        _fsync_document(self._store, JOURNAL_KIND, self._record_id())

    def events(self) -> list[SumpEvent]:
        with self._lock:
            return list(self._events)

    def transitions(self) -> list[SumpEvent]:
        return [event for event in self.events() if event.event in _TRANSITIONS]

    def latest_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def _fsync_document(store: Store, kind: str, record_id: str) -> None:
    """Best effort durability barrier after the atomic store replacement."""

    from tunnelnet.store.record import record_path

    path = record_path(store.root, kind, record_id)
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def read_journal(path: str) -> list[SumpEvent]:
    """Load raw events directly from a journal file (used by tooling)."""

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    raw = payload.get("events", [])
    return [SumpEvent.from_dict(item) for item in raw if isinstance(item, dict)]
