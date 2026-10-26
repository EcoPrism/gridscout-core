"""US EIA retail electricity price provider.

Queries ``electricity/retail-sales`` monthly prices for the site's state and one customer
sector (``COM`` commercial by default; ``RES`` residential, ``IND`` industrial). Prices arrive in
cents/kWh and are stored as USD/kWh. A free API key is required: https://www.eia.gov/opendata/
"""

from __future__ import annotations

from statistics import mean
from typing import List, Optional

from gridscout import cache, settings
from gridscout.models import SeriesPoint, SiteRef, TariffSnapshot
from gridscout.providers import base

EIA_URL = "https://api.eia.gov/v2/electricity/retail-sales/data"
SOURCE = "eia"
SECTOR = "COM"
SECTOR_LABELS = {"COM": "commercial", "RES": "residential", "IND": "industrial", "ALL": "all sectors"}


def state_from_site(site: SiteRef) -> Optional[str]:
    code = (site.state_code or "").upper().strip()
    if not code:
        return None
    return code.split("-")[-1] if "-" in code else code


def compute_trend(series: List[SeriesPoint]) -> Optional[float]:
    """Latest 6 months over the previous 6, as a percent; None with fewer than 12 points.

    This is a semester-over-semester change reported in the ``trend_pct_annual`` field. It is
    not annualised; see the calibration notes in the README.
    """
    if len(series) < 12:
        return None
    recent = mean(p.value for p in series[:6])
    older = mean(p.value for p in series[6:12])
    if older <= 0:
        return None
    return round((recent / older - 1.0) * 100.0, 2)


def parse_response(payload: dict, state: str, sector: str = SECTOR) -> Optional[TariffSnapshot]:
    rows = (payload.get("response") or {}).get("data") or []
    series: List[SeriesPoint] = []
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        try:
            series.append(SeriesPoint(period=str(row.get("period")), value=round(float(price) / 100.0, 5)))
        except (TypeError, ValueError):
            continue
    if not series:
        return None
    series.sort(key=lambda p: p.period, reverse=True)
    series = series[:24]
    label = SECTOR_LABELS.get(sector.upper(), sector.lower())
    return TariffSnapshot(
        currency="USD", current_rate=series[0].value, trend_pct_annual=compute_trend(series),
        series=series, region_label=f"{state} {label}",
    )


class USEIATariffProvider:
    name = SOURCE
    countries = frozenset({"US"})

    def __init__(self, sector: str = SECTOR):
        self.sector = sector.upper()

    async def fetch(self, site: SiteRef) -> base.ProviderResult[TariffSnapshot]:
        state = state_from_site(site)
        if not state:
            return base.unavailable(SOURCE, "no state")
        if not settings.EIA_API_KEY:
            return base.unavailable(SOURCE, "EIA_API_KEY not configured")
        key = cache.tariff_key(SOURCE, f"{state}-{self.sector}")
        cached = await cache.get(key)
        if cached is not None:
            return base.real(SOURCE, TariffSnapshot.model_validate(cached), cached=True)
        params = {
            "api_key": settings.EIA_API_KEY, "frequency": "monthly", "data[0]": "price",
            "facets[stateid][]": state, "facets[sectorid][]": self.sector,
            "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 24,
        }
        try:
            payload = await base.request_json("GET", EIA_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            return base.unavailable(SOURCE, base.failure_note(exc))
        snapshot = parse_response(payload, state, self.sector)
        if snapshot is None:
            return base.unavailable(SOURCE, f"no price rows for {state}")
        await cache.set(key, snapshot.model_dump(mode="json"), cache.TTL_TARIFF)
        return base.real(SOURCE, snapshot)
