"""Deterministic walk through of the protected sump pump controller.

Run with::

    python3 -m tunnelnet.drain.demo /tmp/sump-demo

It exercises every protection rule against a scripted clock and pump and
prints one trace line per water sample, then the drainage/audit report
that is projected from the single durable event journal.
"""

from __future__ import annotations

import sys
import tempfile
import time

from tunnelnet.drain import (
    BLOCKED,
    FAULT_BLOCKED,
    HOLD,
    SumpConfig,
    SumpController,
    build_report,
)
from tunnelnet.store import Store


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def stamp(self) -> str:
        return time.strftime("%H:%M:%SZ", time.gmtime(1_700_000_000 + self.t))


class ScriptedPump:
    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0
        self.fail_start = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("starter contactor open")
        self.starts += 1
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False


def trace(controller: SumpController, clock: Clock, level: float, note: str = "") -> None:
    decision = controller.observe(level)
    window = controller.snapshot()["protection"]
    if window is None:
        protected = "-"
    else:
        remaining = "∞" if window["remaining"] is None else f'{window["remaining"]}s'
        protected = f"{window['reason']}({remaining})"
    print(
        f"t={clock.t:5.0f}s level={level:4.2f} -> {decision.action:15s} "
        f"pump={'ON ' if decision.running else 'off'} protect={protected:22s} {note}"
    )


def main(root: str) -> None:
    clock = Clock()
    pump = ScriptedPump()
    config = SumpConfig(
        start_level=0.80,
        stop_level=0.30,
        emergency_level=1.20,
        min_interval=300.0,
        max_attempts=3,
        backoff_factor=2.0,
    )
    store = Store.open(root)
    ctl = SumpController("pit-1", config, pump, store, clock=clock, stamp=clock.stamp)

    print("== 水面晃动但没过启泵线：泵不动 ==")
    trace(ctl, clock, 0.50)
    trace(ctl, clock, 0.51)
    trace(ctl, clock, 0.49)

    print("== 过启泵线，泵启动；运行中在启泵线附近晃：靠回差保持运行 ==")
    trace(ctl, clock, 0.85)
    trace(ctl, clock, 0.82)
    trace(ctl, clock, 0.79)

    print("== 落到停止线停泵，进入 300s 最小间隔保护 ==")
    trace(ctl, clock, 0.30)

    print("== 保护期内再次过线：只记一次拦截，等保护结束 ==")
    trace(ctl, clock, 0.90)
    trace(ctl, clock, 0.60)
    trace(ctl, clock, 0.90)

    print("== 保护结束后下个采样自动启泵 ==")
    clock.advance(301)
    trace(ctl, clock, 0.90)
    trace(ctl, clock, 0.30)

    print("== 新保护期内水位涨到危急线：安全覆盖，强启 ==")
    clock.advance(60)
    trace(ctl, clock, 1.25)
    trace(ctl, clock, 0.30)

    print("== 启泵失败：退避重试（300s、600s），三次后故障锁，危急线也不能强启 ==")
    pump.fail_start = True
    trace(ctl, clock, 0.90)
    clock.advance(301)  # 第一次退避 300*2^0 = 300
    trace(ctl, clock, 0.90)
    clock.advance(601)  # 第二次退避 300*2^1 = 600
    trace(ctl, clock, 0.90)
    clock.advance(60)
    trace(ctl, clock, 1.30)

    print("== 修泵、人工复位，立刻恢复 ==")
    pump.fail_start = False
    print(f"reset_fault -> {ctl.reset_fault()}")
    trace(ctl, clock, 0.90)
    trace(ctl, clock, 0.30)

    print("== 重启进程：状态与保护从盘上恢复 ==")
    ctl2 = SumpController("pit-1", config, pump, store, clock=clock, stamp=clock.stamp)
    clock.advance(60)
    trace(ctl2, clock, 0.90, "重启后保护仍在")
    clock.advance(301)
    trace(ctl2, clock, 0.90, "保护到期自动恢复启泵")

    print("== 重复启动请求幂等：同一 request 只打一次泵 ==")
    trace(ctl2, clock, 0.30)
    clock.advance(301)
    before = pump.starts
    d1 = ctl2.request_start("req-1")
    d2 = ctl2.request_start("req-1")
    print(f"req-1 两次结果: {d1.action} / {d2.action}，泵实际新增启动 {pump.starts - before} 次")

    print()
    report = build_report(ctl2.journal().events())
    print(f"排水记录 {report.as_dict()['run_count']} 次"
          f"（完成 {report.as_dict()['complete_count']}，应急 {report.as_dict()['emergency_count']}），"
          f"审计条目 {len(report.audit)} 条，均来自同一本事件日志")
    for run in report.runs:
        print("  run", run.as_dict())


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="sump-demo-")
    main(root)
