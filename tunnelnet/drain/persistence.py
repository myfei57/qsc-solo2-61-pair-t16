"""Durable configuration and runtime records for individual sumps."""

from __future__ import annotations

from tunnelnet.store import Store

from .events import CONFIG_KIND, STATE_KIND
from .model import PumpRuntimeState, SumpConfig


def save_config(store: Store, pit_id: str, config: SumpConfig) -> None:
    store.save(CONFIG_KIND, pit_id, config.validated().as_dict())


def load_config(store: Store, pit_id: str) -> SumpConfig | None:
    payload = store.load(CONFIG_KIND, pit_id)
    if payload is None:
        return None
    return SumpConfig.from_dict(payload).validated()


def save_runtime(store: Store, pit_id: str, state: PumpRuntimeState) -> None:
    store.save(STATE_KIND, pit_id, state.as_dict())


def load_runtime(store: Store, pit_id: str) -> PumpRuntimeState | None:
    payload = store.load(STATE_KIND, pit_id)
    if payload is None:
        return None
    return PumpRuntimeState.from_dict(payload)
