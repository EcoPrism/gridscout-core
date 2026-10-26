"""Fallback tariff provider: no data, honestly reported. The assessment proceeds physical-only."""

from __future__ import annotations

from gridscout.models import SiteRef, TariffSnapshot
from gridscout.providers import base


class NullTariffProvider:
    name = "null"
    countries = frozenset()

    async def fetch(self, site: SiteRef) -> base.ProviderResult[TariffSnapshot]:
        return base.unavailable("tariff", f"no tariff provider for {site.country_code or '??'}")
