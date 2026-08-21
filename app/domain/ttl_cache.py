"""Tiny in-process TTL cache for expensive dashboard aggregates."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_STORE: dict[str, tuple[float, object]] = {}


def cache_get(key: str, ttl_sec: float, factory: Callable[[], T]) -> T:
    now = time.monotonic()
    hit = _STORE.get(key)
    if hit is not None and (now - hit[0]) < ttl_sec:
        return hit[1]  # type: ignore[return-value]
    value = factory()
    _STORE[key] = (now, value)
    return value


def cache_clear(prefix: str | None = None) -> None:
    if prefix is None:
        _STORE.clear()
        return
    for key in list(_STORE):
        if key.startswith(prefix):
            del _STORE[key]
