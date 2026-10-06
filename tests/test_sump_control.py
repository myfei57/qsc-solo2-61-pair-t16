from __future__ import annotations

import tempfile
import unittest

from tunnelnet.drain import (
    DrainEventLog,
    PumpUnit,
    PumpRuntimeState,
    SumpConfig,
    SumpControlService,
    SumpController,
    audit_entries,
    extended_until,
    load_runtime,
    save_runtime,
    runs_from_events,
)
from tunnelnet.store import Store


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Valve:
    def __init__(self) -> None:
        self.opens: list[str] = []

    def open_valve(self, cabin_id: str) -> None:
        self.opens.append(cabin_id)


class FailingPump(PumpUnit):
    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def start(self) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("starter fault")
        super().start()


class SilentPump(PumpUnit):
    def is_running(self) -> bool:
        return False


class LateFaultPump(PumpUnit):
    def start(self) -> None:
        super().start()
        raise RuntimeError("late starter fault")


class SumpControlCase(unittest.TestCase):
    def controller(
        self,
        store: Store,
        pump: PumpUnit | None = None,
        valve: Valve | None = None,
        clock: Clock | None = None,
        config: SumpConfig | None = None,
    ) -> tuple[SumpController, Clock]:
        current = clock or Clock(1000.0)
        return (
            SumpController(
                "pit-1",
                config
                or SumpConfig(
                    start_level=0.8,
                    stop_level=0.3,
                    min_interval=300.0,
                    retry_interval=60.0,
                ),
                pump or PumpUnit(),
                store,
                valve=valve,
                cabin_id="cabin-1",
                clock=current,
            ),
            current,
        )

    def test_hysteresis_keeps_pump_running_between_separate_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control, clock = self.controller(Store.open(tmp))
            self.assertEqual(control.sample(0.81, clock()).action, "started")
            clock.value += 10
            self.assertEqual(control.sample(0.5, clock()).action, "running")
            self.assertEqual(control.sample(0.3, clock()).action, "stopped")
            self.assertEqual(control.status().protected, True)

    def test_wait_policy_blocks_restart_until_protection_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            control, clock = self.controller(store)
            control.sample(0.9)
            control.sample(0.2)
            self.assertAlmostEqual(control.protection_remaining(), 300.0)

            blocked = control.sample(0.9, 1010.0)
            self.assertEqual(blocked.action, "blocked")
            self.assertEqual(control.sample(0.9, 1200.0).action, "blocked")
            started = control.sample(0.9, 1300.0)
            self.assertEqual(started.action, "started")
            self.assertGreaterEqual(load_runtime(store, "pit-1").protect_until, 1600.0)

    def test_overlapping_protection_windows_use_later_expiry(self) -> None:
        self.assertEqual(extended_until(1500.0, 1000.0, 300.0), 1500.0)
        self.assertEqual(extended_until(1100.0, 1000.0, 300.0), 1300.0)

    def test_restart_preserves_level_protection_and_starts_after_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            control, clock = self.controller(store)
            control.sample(0.9)
            control.sample(0.2)
            clock.value = 1100.0
            control.sample(0.9)

            restarted_clock = Clock(1200.0)
            restarted, _ = self.controller(store, clock=restarted_clock)
            self.assertEqual(restarted.status().as_dict()["level"], 0.9)
            self.assertEqual(restarted.status().as_dict()["request_status"], "pending")
            self.assertTrue(restarted.status().protected)
            self.assertEqual(restarted.sample(0.9, 1200.0).action, "blocked")
            restarted_clock.value = 1300.0
            self.assertEqual(restarted.sample(0.9, 1300.0).action, "started")

    def test_repeated_start_requests_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            control, clock = self.controller(store)
            first = control.request_start("req-1", 1000.0)
            self.assertEqual(first.action, "started")
            duplicate = control.request_start("req-1", 1001.0)
            self.assertEqual(duplicate.action, "started")
            self.assertEqual(control.events()[1].type, "started")
            requested = [event for event in control.events() if event.type == "requested"]
            self.assertEqual(len(requested), 1)

    def test_failed_start_recovers_after_retry_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            pump = FailingPump(failures=1)
            control, _ = self.controller(store, pump=pump)
            failed = control.request_start("req-1", 1000.0)
            self.assertEqual(failed.action, "failed")
            self.assertTrue(control.status().failed)
            self.assertEqual(control.request_start("req-1", 1030.0).action, "blocked")
            recovered = control.request_start("req-1", 1060.0)
            self.assertEqual(recovered.action, "started")
            self.assertEqual(pump.attempts, 2)

    def test_late_start_failure_stops_pump_and_enters_retry_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pump = LateFaultPump()
            control, _ = self.controller(Store.open(tmp), pump=pump)
            result = control.request_start("req-1", 1000.0)
            self.assertEqual(result.action, "failed")
            self.assertEqual(pump.stops(), 1)
            self.assertFalse(pump.is_running())

    def test_start_without_running_feedback_is_a_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control, _ = self.controller(Store.open(tmp), pump=SilentPump())
            result = control.request_start("req-1", 1000.0)
            self.assertEqual(result.action, "failed")
            self.assertEqual(result.error, "pump did not report running")
            self.assertTrue(control.status().protected)

    def test_starting_pump_is_reconciled_as_running_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            pump = PumpUnit()
            control, _ = self.controller(store, pump=pump)
            control.request_start("req-1", 1000.0)
            save_runtime(
                store,
                "pit-1",
                PumpRuntimeState(
                    status="starting",
                    level=0.9,
                    protect_until=1300.0,
                    request_id="req-1",
                    request_status="starting",
                    last_start=1000.0,
                ),
            )
            restarted, _ = self.controller(store, pump=pump, clock=Clock(1050.0))
            self.assertEqual(restarted.status().running, True)
            self.assertEqual([event.type for event in restarted.events()], [
                "requested",
                "started",
                "recovered",
            ])

    def test_drain_records_and_audit_use_same_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            valve = Valve()
            control, clock = self.controller(store, valve=valve)
            control.sample(0.9, 1000.0)
            clock.value = 1010.0
            control.sample(0.2)
            events = control.events()
            self.assertEqual(valve.opens, ["cabin-1"])
            self.assertEqual([event.type for event in events], ["requested", "started", "stopped"])
            run = runs_from_events(events)[0]
            audit = audit_entries(events)
            self.assertEqual(run.request_id, audit[1]["request_id"])
            self.assertEqual(control.runs()[0].as_dict()["duration"], 10.0)
            self.assertEqual([entry["event"] for entry in audit], [
                "drain_requested",
                "drain_started",
                "drain_stopped",
            ])

    def test_global_event_log_can_be_audited_across_pits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            first, clock = self.controller(store)
            second = SumpController(
                "pit-2",
                SumpConfig(0.9, 0.1),
                PumpUnit(),
                store,
                clock=clock,
            )
            first.request_start("a", 1000.0)
            second.request_start("b", 1000.0)
            log = DrainEventLog(store)
            expected = [
                ("pit-1", "requested"),
                ("pit-1", "started"),
                ("pit-2", "requested"),
                ("pit-2", "started"),
            ]
            self.assertEqual(
                [(event.pit_id, event.type) for event in log.events()],
                expected,
            )
            self.assertEqual([event.id for event in log.events()], [1, 2, 3, 4])
            self.assertEqual(len(audit_entries(log.events())), 4)

    def test_configuration_is_reloaded_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            first, _ = self.controller(
                store,
                config=SumpConfig(
                    start_level=0.7,
                    stop_level=0.4,
                    min_interval=120.0,
                    retry_interval=30.0,
                ),
            )
            first.sample(0.75)
            second, _ = self.controller(store, config=SumpConfig(start_level=0.9, stop_level=0.1))
            self.assertEqual(second.config().start_level, 0.7)
            self.assertEqual(second.config().min_interval, 120.0)

    def test_pending_start_is_withdrawn_when_level_falls_before_protection_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            control, clock = self.controller(store)
            control.sample(0.9)
            control.sample(0.2)
            self.assertEqual(control.sample(0.9, 1010.0).action, "blocked")
            self.assertEqual(control.sample(0.2, 1100.0).action, "withdrawn")
            self.assertEqual(control.status().as_dict()["request_id"], "")

    def test_tick_starts_a_pending_pump_when_protection_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            control, _ = self.controller(store)
            control.sample(0.9, 1000.0)
            control.sample(0.2, 1010.0)
            control.sample(0.9, 1020.0)
            self.assertEqual(control.tick(1200.0)[0].action, "blocked")
            self.assertEqual(control.tick(1310.0)[0].action, "started")

    def test_control_service_fanout_and_shared_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = {}
            service = SumpControlService(
                Store.open(tmp),
                lambda pit_id: pumps.setdefault(pit_id, PumpUnit()),
                clock=Clock(1000.0),
            )
            service.configure("pit-1", cabin_id="cabin-1")
            service.configure("pit-2", SumpConfig(0.9, 0.1), cabin_id="cabin-2")
            service.sample("pit-1", 0.85)
            service.sample("pit-2", 0.95)
            self.assertEqual(service.status("pit-1").running, True)
            self.assertEqual(sorted(pumps), ["pit-1", "pit-2"])
            self.assertEqual([entry["event"] for entry in service.audit_entries()], [
                "drain_requested",
                "drain_started",
                "drain_requested",
                "drain_started",
            ])

    def test_config_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                SumpConfig(start_level=0.3, stop_level=0.8).validated()
            with self.assertRaises(ValueError):
                SumpConfig(start_level=0.8, stop_level=0.3, min_interval=-1).validated()


if __name__ == "__main__":
    unittest.main()
