"""Upstream response cache for providers.

Keys are rounded to three decimals so clustered sites share data-centre and substation lookups.
The default backend is an in-process TTL dictionary. Call :func:`configure` with any object that
has ``async get(key) -> Optional[str]`` and ``async set(key, value: str, ttl_seconds)`` to plug in
Redis or similar, or ``configure(None)`` to disable caching.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

TTL_DATACENTERS = 24 * 3600
TTL_SUBSTATIONS = 24 * 3600
TTL_TARIFF = 7 * 24 * 3600
TTL_GEOCODE = 30 * 24 * 3600


class CacheBackend(Protocol):
    async def get(self, key: str) -> Optional[str]: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class MemoryCache:
    """Process-local TTL cache. Expired entries are dropped lazily on read."""

    def __init__(self) -> None:
        self._items: Dict[str, Tuple[float, str]] = {}

    async def get(self, key: str) -> Optional[str]:
        hit = self._items.get(key)
        if hit is None:
            return None
        expires_at, raw = hit
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return raw

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._items[key] = (time.monotonic() + ttl_seconds, value)

    def clear(self) -> None:
        self._items.clear()


_backend: Optional[CacheBackend] = MemoryCache()


def configure(backend: Optional[CacheBackend]) -> None:
    global _backend
    _backend = backend


def clear() -> None:
    """Drop every entry of the in-memory backend (no-op for other backends)."""
    if isinstance(_backend, MemoryCache):
        _backend.clear()


# -- keys ---------------------------------------------------------------------

def datacenters_key(lat: float, lon: float, radius_km: float) -> str:
    return f"gs:dc:{lat:.3f}:{lon:.3f}:{radius_km:g}"


def substations_key(lat: float, lon: float, radius_km: float) -> str:
    return f"gs:sub:{lat:.3f}:{lon:.3f}:{radius_km:g}"


def tariff_key(provider: str, region: str) -> str:
    return f"gs:tariff:{provider}:{region}"


def geocode_key(address: str) -> str:
    return "gs:geo:" + hashlib.sha1(address.strip().lower().encode("utf-8")).hexdigest()


# -- operations ---------------------------------------------------------------

async def get(key: str) -> Optional[Any]:
    if _backend is None:
        return None
    try:
        raw = await _backend.get(key)
    except Exception as exc:  # noqa: BLE001 - a cache fault must never fail an assessment
        logger.debug("cache get failed for %s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def set(key: str, value: Any, ttl_seconds: int) -> None:  # noqa: A001 - mirrors backend API
    if _backend is None:
        return
    try:
        await _backend.set(key, json.dumps(value, default=str), ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache set failed for %s: %s", key, exc)
