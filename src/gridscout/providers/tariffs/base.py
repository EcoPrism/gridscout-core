"""Tariff provider contract and registry (LLD §5.4)."""

from __future__ import annotations

from typing import Dict, FrozenSet, Protocol

from gridscout.models import SiteRef, TariffSnapshot
from gridscout.providers import base


class TariffProvider(Protocol):
    name: str
    countries: FrozenSet[str]

    async def fetch(self, site: SiteRef) -> base.ProviderResult[TariffSnapshot]: ...


REGISTRY: Dict[str, TariffProvider] = {}


def register(provider: TariffProvider) -> None:
    for cc in provider.countries:
        REGISTRY[cc.upper()] = provider


def provider_for(country_code: str) -> TariffProvider:
    from .null import NullTariffProvider

    return REGISTRY.get((country_code or "").upper(), NullTariffProvider())
