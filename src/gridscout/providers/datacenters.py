"""PeeringDB facility provider -> list[Threat] (LLD §5.3)."""

from __future__ import annotations

import math
from typing import List, Optional

from gridscout import settings
from gridscout import cache, scoring
from gridscout.models import Threat
from gridscout.providers import base

PEERINGDB_FAC_URL = "https://www.peeringdb.com/api/fac"
SOURCE = "peeringdb"


class PeeringDBProvider:
    name = SOURCE

    async def fetch(self, lat: float, lon: float, radius_km: Optional[float] = None) -> base.ProviderResult[List[Threat]]:
        radius = radius_km or settings.RADIUS_KM
        max_threats = settings.MAX_THREATS
        key = cache.datacenters_key(lat, lon, radius)
        cached = await cache.get(key)
        if cached is not None:
            return base.real(SOURCE, base.model_list(cached, Threat), cached=True)

        lat_r = radius / 111.0
        lon_r = radius / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
        params = {
            "latitude__gt": lat - lat_r, "latitude__lt": lat + lat_r,
            "longitude__gt": lon - lon_r, "longitude__lt": lon + lon_r,
            "limit": 250,
        }
        headers = {}
        if settings.PEERINGDB_API_KEY:
            headers["Authorization"] = f"Api-Key {settings.PEERINGDB_API_KEY}"  # the POC read the key but never sent it
        try:
            payload = await base.request_json("GET", PEERINGDB_FAC_URL, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return base.unavailable(SOURCE, base.failure_note(exc))

        threats = parse_facilities(payload.get("data", []), lat, lon, radius)
        threats = scoring.cap_threats(threats, max_threats)
        await cache.set(key, base.dump_list(threats), cache.TTL_DATACENTERS)
        return base.real(SOURCE, threats)


def parse_facilities(rows: list, lat: float, lon: float, radius_km: float) -> List[Threat]:
    threats: List[Threat] = []
    for fac in rows:
        f_lat, f_lon = fac.get("latitude"), fac.get("longitude")
        if f_lat is None or f_lon is None:
            continue
        d = scoring.haversine_km(lat, lon, float(f_lat), float(f_lon))
        if d > radius_km:
            continue
        name = fac.get("name") or ""
        operator = fac.get("org_name") or ""
        cls, load = scoring.classify_facility(name, operator)
        threats.append(Threat(
            id=f"pdb-{fac.get('id')}", name=name, operator=operator, facility_class=cls, estimated_load_mw=load,
            distance_km=round(d, 2), lat=float(f_lat), lon=float(f_lon), threat_level=scoring.threat_level(cls, d),
        ))
    threats.sort(key=lambda t: t.distance_km)
    return threats
