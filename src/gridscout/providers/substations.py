"""OpenStreetMap Overpass substation provider -> list[Substation], nearest first (LLD §5.3).

One query per assessment at ``max(16, 2 x radius)`` km so the shared-substation analysis has
enough candidates. A module-level semaphore of 2 keeps the sweep under the public instance's
rate limits; ``GRIDSCOUT_OVERPASS_URL`` can point at another mirror.
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from gridscout import settings
from gridscout import cache, scoring
from gridscout.models import Substation
from gridscout.providers import base

SOURCE = "overpass"
_SEMAPHORE = asyncio.Semaphore(2)
_NUM = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def query_radius_km(radius_km: float) -> float:
    return max(16.0, 2.0 * radius_km)


def build_query(lat: float, lon: float, radius_km: float) -> str:
    r_m = int(radius_km * 1000)
    return (
        "[out:json][timeout:25];\n(\n"
        f'  node["power"="substation"](around:{r_m},{lat},{lon});\n'
        f'  way["power"="substation"](around:{r_m},{lat},{lon});\n'
        f'  relation["power"="substation"](around:{r_m},{lat},{lon});\n'
        ");\nout center tags;"
    )


class OverpassProvider:
    name = SOURCE

    async def fetch(self, lat: float, lon: float, radius_km: Optional[float] = None) -> base.ProviderResult[List[Substation]]:
        q_radius = query_radius_km(radius_km or settings.RADIUS_KM)
        key = cache.substations_key(lat, lon, q_radius)
        cached = await cache.get(key)
        if cached is not None:
            return base.real(SOURCE, base.model_list(cached, Substation), cached=True)
        try:
            async with _SEMAPHORE:
                payload = await base.request_json("POST", settings.OVERPASS_URL, data={"data": build_query(lat, lon, q_radius)})
        except Exception as exc:  # noqa: BLE001
            return base.unavailable(SOURCE, base.failure_note(exc))
        subs = parse_elements(payload.get("elements", []), lat, lon)
        await cache.set(key, base.dump_list(subs), cache.TTL_SUBSTATIONS)
        return base.real(SOURCE, subs)


def parse_voltage(raw: Optional[str]) -> Optional[int]:
    """``"138000;230000"`` -> 230 (kV). Takes the highest value; values above 1000 are volts."""
    if not raw:
        return None
    values = []
    for part in str(raw).split(";"):
        m = _NUM.search(part)
        if not m:
            continue
        v = float(m.group(0))
        values.append(v / 1000.0 if v > 1000 else v)
    if not values:
        return None
    return int(round(max(values)))


def parse_elements(elements: list, lat: float, lon: float) -> List[Substation]:
    out: List[Substation] = []
    for el in elements:
        if el.get("type") == "node":
            e_lat, e_lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            e_lat, e_lon = center.get("lat"), center.get("lon")
        if e_lat is None or e_lon is None:
            continue
        tags = el.get("tags") or {}
        out.append(Substation(
            id=f"osm-{el.get('type')}-{el.get('id')}",
            name=tags.get("name") or tags.get("ref") or "Unnamed substation",
            owner=tags.get("operator"),
            voltage_kv=parse_voltage(tags.get("voltage")),
            distance_km=round(scoring.haversine_km(lat, lon, float(e_lat), float(e_lon)), 2),
            lat=float(e_lat), lon=float(e_lon),
        ))
    out.sort(key=lambda s: s.distance_km)
    return out
