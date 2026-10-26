"""Rate projection and cost exposure (LLD §5.6 financial terms, §5.7 five-year cost)."""

from __future__ import annotations

from datetime import date
from typing import Optional

from .models import RateProjection, SeriesPoint, TariffSnapshot

DEFAULT_ANNUAL_MWH = 120.0


def _add_months(start: date, months: int) -> date:
    y, m = divmod(start.month - 1 + months, 12)
    return date(start.year + y, m + 1, 1)


def rate_projection(
    tariff: TariffSnapshot,
    annual_increase_pct: float,
    infra_impact_pct_3yr: float,
    annual_mwh: Optional[float],
    default_annual_mwh: float = DEFAULT_ANNUAL_MWH,
    start: Optional[date] = None,
) -> RateProjection:
    """12 monthly points ``rate x (1 + incr/100 x m/12)`` for m = 1..12, plus annual exposure.

    Exposure is always computed; when ``annual_mwh`` is unknown the configured default is used
    and ``annual_mwh_is_default`` is set so the UI can flag the assumption.
    """
    start = start or date.today().replace(day=1)
    rate = tariff.current_rate
    monthly = [
        SeriesPoint(period=_add_months(start, m).strftime("%Y-%m"), value=round(rate * (1 + annual_increase_pct / 100.0 * m / 12.0), 5))
        for m in range(1, 13)
    ]
    use_default = annual_mwh is None or annual_mwh <= 0
    mwh = default_annual_mwh if use_default else float(annual_mwh)
    exposure = round(mwh * 1000.0 * rate * annual_increase_pct / 100.0, 2)
    return RateProjection(
        infra_impact_pct_3yr=infra_impact_pct_3yr,
        annual_increase_pct=annual_increase_pct,
        year_end_rate=round(rate * (1 + annual_increase_pct / 100.0), 5),
        monthly=monthly,
        annual_exposure=exposure,
        assumed_annual_mwh=mwh,
        annual_mwh_is_default=use_default,
    )


def cost_5yr(current_rate: float, annual_increase_pct: float, annual_mwh: float) -> float:
    """Total electricity cost over five years with the rate compounding annually."""
    total = 0.0
    for year in range(5):
        total += annual_mwh * 1000.0 * current_rate * (1 + annual_increase_pct / 100.0) ** year
    return round(total, 2)
