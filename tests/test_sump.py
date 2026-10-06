"""Tests for hysteresis, protection windows and the sump controller."""

from __future__ import annotations

import tempfile
import unittest

from tunnelnet.drain import (
    BLOCKED,
    COOLDOWN,
    FAULT,
    FAULT_BLOCKED,
    HOLD,
    Protection,
    START,
    START_EMERGENCY,
    START_FAILURE,
    STOP,
    SumpConfig,
    SumpController,
    Window,
    audit_entries,
    build_report,
    decide,
    drainage_runs,
)
from tunnelnet.drain.journal import (
    EV_FAULT_ALARM,
    EV_PROTECTION,
    EV_RECOVERED,
    EV_RESET,
    EV_START,
    EV_START_BLOCKED,
    EV_START_EMERGENCY,
    EV_START_FAILED,
    EV_STOP,
)
from tunnelnet.store import Store

CONFIG = SumpConfig(
    start_level=0.8,
    stop_level=0.3,
    emergency_level=1.2,
    min_interval=300.0,
    max_attempts=3,
    backoff_factor=2.0,
)


class FakePump:
    def __init__(self, fail: bool = False) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0
        self.fail_start = fail

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("dead")
        self.starts += 1
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class PolicyCase(unittest.TestCase):
    def test_levels_must_be_ordered(self) -> None:
        with self.assertRaises(ValueError):
            SumpConfig(0.3, 0.8, 1.2, 300.0)
        with self.assertRaises(ValueError):
            SumpConfig(0.8, 0.3, 0.5, 300.0)
        with self.assertRaises(ValueError):
            SumpConfig(0.8, 0.3, 1.2, -1.0)

    def test_hysteresis_does_not_chatter(self) -> None:
        none = Protection()
        # Below the start level: hold even when wobbling above the stop level.
        self.assertEqual(decide(level=0.5, running=False, config=CONFIG, protection=none, now=0), HOLD)
        # At or above the start level: start.
        self.assertEqual(decide(level=0.8, running=False, config=CONFIG, protection=none, now=0), START)
        # Running between stop and start levels: keep running (hysteresis band).
        self.assertEqual(decide(level=0.7, running=True, config=CONFIG, protection=none, now=0), HOLD)
        # At the stop level: stop.
        self.assertEqual(decide(level=0.3, running=True, config=CONFIG, protection=none, now=0), STOP)

    def test_cooldown_blocks_normal_start_but_expires(self) -> None:
        protected = Protection(Window(COOLDOWN, until=400.0))
        action = decide(level=0.9, running=False, config=CONFIG, protection=protected, now=100.0)
        self.assertEqual(action, BLOCKED)
        self.assertAlmostEqual(protected.window.remaining(100.0), 300.0)
        # At the exact end time the window is inactive.
        self.assertEqual(decide(level=0.9, running=False, config=CONFIG, protection=protected, now=400.0), START)

    def test_emergency_level_overrides_time_windows_only(self) -> None:
        cooldown = Protection(Window(COOLDOWN, until=400.0))
        self.assertEqual(
            decide(level=1.2, running=False, config=CONFIG, protection=cooldown, now=100.0),
            START_EMERGENCY,
        )
        backoff = Protection(Window(START_FAILURE, until=400.0))
        self.assertEqual(
            decide(level=1.3, running=False, config=CONFIG, protection=backoff, now=100.0),
            START_EMERGENCY,
        )
        fault = Protection(Window(FAULT, float("inf")))
        self.assertEqual(
            decide(level=1.3, running=False, config=CONFIG, protection=fault, now=100.0),
            FAULT_BLOCKED,
        )

    def test_overlapping_windows_merge_by_latest_end_not_sum(self) -> None:
        merged = Protection(Window(COOLDOWN, until=400.0)).engage(START_FAILURE, 500.0)
        self.assertEqual(merged.window.reason, START_FAILURE)
        self.assertEqual(merged.window.until, 500.0)
        # An earlier overlapping window cannot shorten the current one.
        merged = merged.engage(COOLDOWN, 450.0)
        self.assertEqual(merged.window.reason, START_FAILURE)
        self.assertEqual(merged.window.until, 500.0)
        # A fault lock dominates any time based window.
        merged = merged.engage(FAULT, float("inf"))
        self.assertTrue(merged.is_fault())
        self.assertEqual(merged.engage(COOLDOWN, 9999.0).window.until, float("inf"))

    def test_backoff_grows_and_is_capped(self) -> None:
        self.assertEqual(CONFIG.backoff(1000.0, 1), 1300.0)
        self.assertEqual(CONFIG.backoff(1000.0, 2), 1600.0)
        cap_config = SumpConfig(0.8, 0.3, 1.2, 300.0, backoff_cap=500.0)
        self.assertEqual(cap_config.backoff(1000.0, 2), 1500.0)


class ControllerCase(unittest.TestCase):
    def _controller(self, pump: FakePump | None = None, fail: bool = False):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = Store.open(self._tmp.name)
        clock = Clock()
        ctl = SumpController(
            "pit-1", CONFIG, pump or FakePump(fail=fail), store,
            clock=clock, stamp=lambda: f"t{clock.t}",
        )
        return ctl, store, clock

    def test_hysteresis_cycle_and_cooldown(self) -> None:
        ctl, _, clock = self._controller()
        ctl.observe(0.5)
        self.assertFalse(ctl.running())
        d = ctl.observe(0.8)
        self.assertEqual(d.action, START)
        self.assertTrue(ctl.running())
        # Wobbling inside the band keeps the pump on.
        self.assertEqual(ctl.observe(0.79).action, HOLD)
        self.assertEqual(ctl.observe(0.3).action, STOP)
        self.assertFalse(ctl.running())
        # A high level inside the cooldown is blocked.
        self.assertEqual(ctl.observe(0.9).action, BLOCKED)
        # Further high samples are not logged again (one edge, not per wave).
        blocked_events = [e for e in ctl.journal().events() if e.event == EV_START_BLOCKED]
        self.assertEqual(len(blocked_events), 1)
        self.assertEqual(ctl.observe(0.95).action, BLOCKED)
        self.assertEqual(len([e for e in ctl.journal().events() if e.event == EV_START_BLOCKED]), 1)
        # Falling back re-arms the edge log.
        ctl.observe(0.2)
        clock.advance(299)
        self.assertEqual(ctl.observe(0.9).action, BLOCKED)
        self.assertEqual(len([e for e in ctl.journal().events() if e.event == EV_START_BLOCKED]), 2)
        # Once the interval elapsed the pump starts on the next sample.
        clock.advance(2)
        self.assertEqual(ctl.observe(0.9).action, START)

    def test_emergency_override_inside_cooldown(self) -> None:
        ctl, _, clock = self._controller()
        ctl.observe(0.8)
        ctl.observe(0.3)
        clock.advance(60)
        d = ctl.observe(1.2)
        self.assertEqual(d.action, START_EMERGENCY)
        self.assertTrue(ctl.running())
        names = [e.event for e in ctl.journal().events()]
        self.assertIn(EV_START_EMERGENCY, names)

    def test_failed_starts_backoff_then_recover(self) -> None:
        pump = FakePump(fail=True)
        ctl, _, clock = self._controller(pump=pump)
        d1 = ctl.observe(0.9)
        self.assertEqual(d1.action, BLOCKED)
        self.assertEqual(d1.protection_reason, START_FAILURE)
        self.assertEqual(ctl.snapshot()["failed_attempts"], 1)
        # Backoff honoured, then the next attempt succeeds.
        clock.advance(301)
        pump.fail_start = False
        d2 = ctl.observe(0.9)
        self.assertEqual(d2.action, START)
        self.assertEqual(ctl.snapshot()["failed_attempts"], 0)

    def test_three_failures_fault_lock_needs_manual_reset(self) -> None:
        pump = FakePump(fail=True)
        ctl, _, clock = self._controller(pump=pump)
        ctl.observe(0.9)  # backoff 300
        clock.advance(301)
        ctl.observe(0.9)  # backoff 600
        clock.advance(601)
        d = ctl.observe(0.9)  # third failure -> fault lock
        self.assertEqual(d.action, FAULT_BLOCKED)
        self.assertTrue(ctl.protection().is_fault())
        # Neither time nor the emergency level can bypass the fault lock.
        clock.advance(100000)
        self.assertEqual(ctl.observe(1.3).action, FAULT_BLOCKED)
        alarms = [e for e in ctl.journal().events() if e.event == EV_FAULT_ALARM]
        self.assertEqual(len(alarms), 1)
        # Idempotent reset.
        self.assertTrue(ctl.reset_fault())
        self.assertFalse(ctl.reset_fault())
        pump.fail_start = False
        self.assertEqual(ctl.observe(0.9).action, START)
        self.assertIn(EV_RESET, [e.event for e in ctl.journal().events()])

    def test_protection_and_running_state_survive_restart(self) -> None:
        tmp = tempfile.mkdtemp()
        store = Store.open(tmp)
        clock = Clock()
        pump = FakePump()
        ctl = SumpController("pit-1", CONFIG, pump, store, clock=clock, stamp=lambda: f"t{clock.t}")
        ctl.observe(0.8)
        ctl.observe(0.3)  # cooldown engaged until 1600
        clock.advance(100)

        reborn_pump = FakePump()
        reborn = SumpController("pit-1", CONFIG, reborn_pump, store, clock=clock, stamp=lambda: f"t{clock.t}")
        self.assertFalse(reborn.running())
        self.assertEqual(reborn.observe(0.9).action, BLOCKED)
        names = [e.event for e in reborn.journal().events()]
        self.assertIn(EV_RECOVERED, names)

        # A running pump is also trusted across a restart and still stops.
        clock.advance(201)
        reborn.observe(0.9)  # START
        reborn2 = SumpController("pit-1", CONFIG, FakePump(), store, clock=clock, stamp=lambda: f"t{clock.t}")
        self.assertTrue(reborn2.running())
        self.assertEqual(reborn2.observe(0.3).action, STOP)

    def test_duplicate_start_request_is_idempotent(self) -> None:
        ctl, _, clock = self._controller()
        ctl.observe(0.2)
        clock.advance(400)  # no window
        first = ctl.request_start("req-1")
        second = ctl.request_start("req-1")
        self.assertEqual((first.action, second.action), (START, START))
        pump = ctl._pump
        self.assertEqual(pump.starts, 1)
        # A different request id is a new command: while running it simply
        # evaluates current level (no second pump start).
        ctl.observe(0.9)
        self.assertEqual(ctl.request_start("req-2").action, HOLD)
        self.assertEqual(pump.starts, 1)

    def test_idempotency_cache_survives_restart(self) -> None:
        tmp = tempfile.mkdtemp()
        store = Store.open(tmp)
        clock = Clock()
        pump = FakePump()
        ctl = SumpController("pit-1", CONFIG, pump, store, clock=clock, stamp=lambda: f"t{clock.t}")
        clock.advance(400)
        self.assertEqual(ctl.request_start("req-9").action, START)
        reborn = SumpController("pit-1", CONFIG, pump, store, clock=clock, stamp=lambda: f"t{clock.t}")
        # Pump still running from before; the cached decision replays verbatim
        # and must not touch the actuator again.
        calls = pump.starts
        self.assertEqual(reborn.request_start("req-9").action, START)
        self.assertEqual(pump.starts, calls)

    def test_manual_request_still_respects_cooldown(self) -> None:
        ctl, _, clock = self._controller()
        ctl.observe(0.8)
        ctl.observe(0.3)
        clock.advance(10)
        d = ctl.request_start("manual-1")
        self.assertEqual(d.action, BLOCKED)

    def test_state_document_is_json_serialisable_with_fault_lock(self) -> None:
        import json

        pump = FakePump(fail=True)
        ctl, _, clock = self._controller(pump=pump)
        for gap in (0.0, 301.0, 601.0):
            clock.advance(gap)
            ctl.observe(0.9)
        snapshot = ctl.snapshot()
        json.dumps(snapshot)  # inf remaining must be rendered as null first
        self.assertIsNone(snapshot["protection"]["remaining"])
        self.assertIsNone(snapshot["protection"]["until"])

    def test_single_journal_feeds_both_projections(self) -> None:
        ctl, _, clock = self._controller()
        ctl.observe(0.85)
        ctl.observe(0.3)
        clock.advance(100)  # still inside the cooldown window
        ctl.observe(1.25)  # emergency override starts the second run
        ctl.observe(0.3)
        events = ctl.journal().events()
        report = build_report(events)
        self.assertEqual(len(report.runs), 2)
        self.assertFalse(report.runs[0].open)
        self.assertTrue(report.runs[1].emergency)
        # The audit projection references the exact same event sequences.
        run_starts = [run.start_seq for run in report.runs]
        audit_starts = [
            e.seq for e in report.audit
            if e.event.startswith((EV_START + ":", EV_START_EMERGENCY + ":"))
        ]
        self.assertEqual(run_starts, audit_starts)
        entries = audit_entries(events)
        self.assertTrue(all(entry.id.startswith("pit-1-") for entry in entries))
        # A crash dangling run is reported open, not dropped.
        dangling = drainage_runs([
            _event(1, EV_START, 0.9),
        ])
        self.assertEqual(len(dangling), 1)
        self.assertTrue(dangling[0].open)
        self.assertEqual(dangling[0].as_dict()["status"], "running")

    def test_journal_appended_transitions(self) -> None:
        ctl, _, _ = self._controller()
        ctl.observe(0.9)
        names = [e.event for e in ctl.journal().events()]
        self.assertEqual(names, [EV_START])
        ctl.observe(0.3)
        self.assertEqual(
            [e.event for e in ctl.journal().events()],
            [EV_START, EV_STOP, EV_PROTECTION],
        )

    def test_stop_failure_keeps_running_assumption(self) -> None:
        class FailStopPump(FakePump):
            def stop(self) -> None:
                self.stops += 1
                raise RuntimeError("welded contactor")

        ctl, _, _ = self._controller(pump=FailStopPump())
        ctl.observe(0.9)
        ctl.observe(0.3)
        self.assertTrue(ctl.running())
        from tunnelnet.drain.journal import EV_STOP_FAILED

        self.assertIn(EV_STOP_FAILED, [e.event for e in ctl.journal().events()])


def _event(seq: int, name: str, level: float) -> object:
    from tunnelnet.drain.journal import SumpEvent

    return SumpEvent(seq=seq, at=f"t{seq}", event=name, pit_id="pit-1", level=level, running=name == EV_START)


if __name__ == "__main__":
    unittest.main()
