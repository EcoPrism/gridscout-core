"""Pydantic models for the GridScout grid-risk module (LLD §4.2, schema_version 2).

Kept OpenAPI-3.0.2-safe (``Optional[X]`` rather than ``X | None``, no tuples) so the models can be
served by API gateways that still speak OpenAPI 3.0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ProvenanceStatus = Literal["real", "cached", "estimated", "unavailable"]
FacilityClass = Literal["hyperscale", "enterprise", "edge"]
ThreatLevel = Literal["critical", "high", "medium", "low"]
Band = Literal["critical", "high", "medium", "low"]
Coverage = Literal["full", "partial", "physical_only"]


class Provenance(BaseModel):
    source: str  # "peeringdb" | "overpass" | "eia" | "nominatim" | "caller" | narrator name
    status: ProvenanceStatus
    fetched_at: Optional[datetime] = None
    note: Optional[str] = None  # e.g. "Overpass 429 after 3 retries"


class SiteRef(BaseModel):
    site_key: str
    name: str
    address: str  # "AddressLine1, City, PostalCode, Country"
    country_code: str  # ISO-3166-1 alpha-2, upper
    state_code: Optional[str] = None  # ISO-3166-2 subdivision (US state, DE Land...)
    lat: Optional[float] = None
    lon: Optional[float] = None
    site_type: Optional[str] = None
    supplier_name: Optional[str] = None
    annual_mwh: Optional[float] = None  # latest full year, when the caller knows it
    is_active: bool = True
    is_adhoc: bool = False


class Threat(BaseModel):
    """One data centre near the site."""

    id: str
    name: str
    operator: str
    facility_class: FacilityClass
    estimated_load_mw: float  # class constant, see scoring.py
    distance_km: float
    lat: float
    lon: float
    threat_level: ThreatLevel


class Substation(BaseModel):
    id: str
    name: str
    owner: Optional[str] = None
    voltage_kv: Optional[int] = None
    distance_km: float
    lat: float
    lon: float


class SeriesPoint(BaseModel):
    period: str  # "2026-06"
    value: float


class TariffSnapshot(BaseModel):
    currency: str  # "USD" | "EUR" | "INR"
    unit: Literal["per_kwh"] = "per_kwh"
    current_rate: float  # currency per kWh (not cents)
    trend_pct_annual: Optional[float] = None  # provider-computed
    series: List[SeriesPoint] = Field(default_factory=list)  # newest first, <= 24 points
    region_label: str  # "Virginia commercial" | "DE non-household band IC"


class SectionScores(BaseModel):
    physical: float  # 0-100
    shared_infra: float  # 0-100
    financial: Optional[float] = None  # 0-100, None when tariff unavailable
    composite: float
    band: Band
    coverage: Coverage


class RateProjection(BaseModel):
    infra_impact_pct_3yr: float
    annual_increase_pct: float
    year_end_rate: float
    monthly: List[SeriesPoint]  # 12 projected points
    annual_exposure: Optional[float] = None  # currency/year
    assumed_annual_mwh: float  # actual, or the configured default when unknown
    annual_mwh_is_default: bool = False


class Narrative(BaseModel):
    synthesis: str
    recommendations: List[str]  # exactly 3
    key_findings: List[str]
    model: str
    region_prompt: str  # "us" | "eu" | "in" | "generic"


class AssessmentResult(BaseModel):
    schema_version: Literal[2] = 2
    assessment_id: str  # uuid4
    site: SiteRef
    radius_km: float
    threats: List[Threat]
    total_capacity_mw: float
    substation: Optional[Substation] = None
    tariff: Optional[TariffSnapshot] = None
    scores: SectionScores
    projection: Optional[RateProjection] = None
    narrative: Optional[Narrative] = None
    provenance: Dict[str, Provenance]  # keys: coordinates, datacenters, substations, tariff, narrative
    created_at: datetime
    duration_ms: int
