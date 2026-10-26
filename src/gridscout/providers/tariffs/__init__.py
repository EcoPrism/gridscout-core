"""Tariff providers keyed by ISO country code. Unknown countries fall back to NullTariffProvider."""

from .base import REGISTRY, TariffProvider, provider_for, register
from .null import NullTariffProvider
from .us_eia import USEIATariffProvider

register(USEIATariffProvider())

__all__ = ["REGISTRY", "TariffProvider", "provider_for", "register", "NullTariffProvider", "USEIATariffProvider"]
