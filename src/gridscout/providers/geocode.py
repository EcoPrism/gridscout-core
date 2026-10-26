"""Nominatim geocoder for ad-hoc addresses (LLD §5.3).

geopy is synchronous, so the call runs in a thread. A semaphore of 1 plus 1.1 s spacing honours
the Nominatim usage policy of one request per second. Results are cached for 30 days.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from gridscout import cache, settings
from gridscout.providers import base

SOURCE = "nominatim"
USER_AGENT = settings.NOMINATIM_USER_AGENT
MIN_SPACING_S = 1.1
_SEMAPHORE = asyncio.Semaphore(1)
_last_call = 0.0


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    country_code: str  # ISO-3166-1 alpha-2, upper
    state_code: Optional[str]  # ISO-3166-2, e.g. "US-VA", "DE-HE"
    display_name: str


def _geocode_sync(address: str, country_code: Optional[str]) -> Optional[Dict[str, Any]]:
    """Blocking geopy call; patched in tests."""
    from geopy.geocoders import Nominatim

    geocoder = Nominatim(user_agent=USER_AGENT, timeout=10)
    kwargs: Dict[str, Any] = {"addressdetails": True, "exactly_one": True}
    if country_code:
        kwargs["country_codes"] = country_code.lower()
    loc = geocoder.geocode(address, **kwargs)
    if loc is None:
        return None
    return {"lat": loc.latitude, "lon": loc.longitude, "raw": loc.raw}


def parse_raw(raw: Dict[str, Any], lat: float, lon: float) -> GeocodeResult:
    addr = raw.get("address") or {}
    cc = (addr.get("country_code") or "").upper()
    state = addr.get("ISO3166-2-lvl4") or addr.get("ISO3166-2-lvl6")
    return GeocodeResult(lat=float(lat), lon=float(lon), country_code=cc, state_code=state, display_name=raw.get("display_name") or "")


class NominatimGeocoder:
    name = SOURCE

    async def fetch(self, address: str, country_code: Optional[str] = None) -> base.ProviderResult[GeocodeResult]:
        global _last_call
        key = cache.geocode_key(f"{address}|{country_code or ''}")
        cached = await cache.get(key)
        if cached is not None:
            return base.real(SOURCE, GeocodeResult(**cached), cached=True)
        try:
            async with _SEMAPHORE:
                wait = MIN_SPACING_S - (time.monotonic() - _last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_call = time.monotonic()
                hit = await asyncio.to_thread(_geocode_sync, address, country_code)
        except Exception as exc:  # noqa: BLE001
            return base.unavailable(SOURCE, base.failure_note(exc))
        if hit is None:
            return base.unavailable(SOURCE, "address not found")
        result = parse_raw(hit["raw"], hit["lat"], hit["lon"])
        await cache.set(key, asdict(result), cache.TTL_GEOCODE)
        return base.real(SOURCE, result)
