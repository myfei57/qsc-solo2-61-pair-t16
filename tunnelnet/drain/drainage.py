"""Drainage records and audit entries are projections of one event journal.

Nothing here stores facts of its own: every drainage run and every audit
entry is derived from the same ordered list of
:class:`tunnelnet.drain.journal.SumpEvent`, so the drainage report an
operator sees and the audit reference an inspector follows can never
diverge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    SumpEvent,
)

# Events that close the operational view of a drainage run.
RUN_CLOSING = (EV_STOP, EV_START_FAILED, EV_STOP_FAILED)
# Audit events, in order of severity, surfaced from the same journal.
AUDIT_EVENTS = (
    EV_START,
    EV_START_EMERGENCY,
    EV_STOP,
    EV_START_BLOCKED,
    EV_START_FAILED,
    EV_STOP_FAILED,
    EV_FAULT_ALARM,
    EV_PROTECTION,
    EV_RESET,
    EV_RECOVERED,
)

# Stable human readable wording for audit output.
EVENT_WORDING = {
    EV_START: "pump started on demand",
    EV_START_EMERGENCY: "pump started on emergency override",
    EV_STOP: "pump stopped at stop level",
    EV_START_BLOCKED: "start held back by protection window",
    EV_START_FAILED: "pump start failed",
    EV_STOP_FAILED: "pump stop failed",
    EV_FAULT_ALARM: "fault lock alarm raised",
    EV_PROTECTION: "protection window engaged",
    EV_RESET: "fault lock manually reset",
    EV_RECOVERED: "controller recovered durable state after restart",
}


@dataclass(frozen=True)
class DrainageRun:
    """One start/stop pair (a still running pump has ``ended_at`` empty)."""

    pit_id: str
    started_at: str
    ended_at: str = ""
    start_level: float | None = None
    end_level: float | None = None
    emergency: bool = False
    start_seq: int = 0
    end_seq: int | None = None

    @property
    def open(self) -> bool:
        return not self.ended_at

    def as_dict(self) -> dict[str, object]:
        return {
            "pit_id": self.pit_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "start_level": self.start_level,
            "end_level": self.end_level,
            "emergency": self.emergency,
            "status": "running" if self.open else "complete",
            "journal_seq_start": self.start_seq,
            "journal_seq_end": self.end_seq,
        }


@dataclass(frozen=True)
class AuditEntry:
    """One audit view row, pointing straight back at the journal event."""

    id: str
    seq: int
    at: str
    event: str
    pit_id: str
    level: float | None
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "seq": self.seq,
            "at": self.at,
            "event": self.event,
            "wording": EVENT_WORDING.get(self.event, self.event),
            "pit_id": self.pit_id,
            "level": self.level,
            "detail": self.detail,
        }


def drainage_runs(events: list[SumpEvent]) -> list[DrainageRun]:
    """Pair start and stop events; tolerate failures and crashes.

    A start failure never opens a run. The journal belongs to one pit and
    the controller runs at most one pump, so at most one run is open at any
    time; a closing event ends that run.
    """

    runs: list[DrainageRun] = []
    open_index: int | None = None
    for event in events:
        if event.event in (EV_START, EV_START_EMERGENCY):
            if open_index is not None:  # restart gap left a run dangling
                runs[open_index] = _close(runs[open_index], event)
            runs.append(
                DrainageRun(
                    pit_id=event.pit_id,
                    started_at=event.at,
                    start_level=event.level,
                    emergency=event.event == EV_START_EMERGENCY,
                    start_seq=event.seq,
                )
            )
            open_index = len(runs) - 1
        elif event.event in RUN_CLOSING and open_index is not None:
            runs[open_index] = _close(runs[open_index], event)
            open_index = None
    return runs


def _close(run: DrainageRun, event: SumpEvent) -> DrainageRun:
    return DrainageRun(
        pit_id=run.pit_id,
        started_at=run.started_at,
        ended_at=event.at,
        start_level=run.start_level,
        end_level=event.level,
        emergency=run.emergency,
        start_seq=run.start_seq,
        end_seq=event.seq,
    )


def audit_entries(events: list[SumpEvent]) -> list[AuditEntry]:
    """Project the same events auditors reference, one row per transition."""

    entries = []
    for event in events:
        if event.event not in AUDIT_EVENTS:
            continue
        reason = f":{event.reason}" if event.reason else ""
        entries.append(
            AuditEntry(
                id=f"{event.pit_id}-{event.seq:012d}",
                seq=event.seq,
                at=event.at,
                event=event.event + reason,
                pit_id=event.pit_id,
                level=event.level,
                detail=dict(event.detail),
            )
        )
    return entries


@dataclass(frozen=True)
class SumpReport:
    """One read model built once from the single journal."""

    runs: list[DrainageRun]
    audit: list[AuditEntry]

    def as_dict(self) -> dict[str, object]:
        return {
            "runs": [run.as_dict() for run in self.runs],
            "audit": [entry.as_dict() for entry in self.audit],
            "run_count": len(self.runs),
            "complete_count": sum(1 for run in self.runs if not run.open),
            "emergency_count": sum(1 for run in self.runs if run.emergency),
        }


def build_report(events: list[SumpEvent]) -> SumpReport:
    """Build drainage and audit projections from the SAME event list."""

    return SumpReport(runs=drainage_runs(events), audit=audit_entries(events))
