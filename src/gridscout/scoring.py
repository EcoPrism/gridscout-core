"""Pure scoring functions for GridScout (LLD §5.6).

No I/O, no config reads at call time: every threshold is a module constant so tests are
deterministic. Constants default to the original proof-of-concept values
so the reference assessments in the README reproduce.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import Band, Coverage, FacilityClass, ProvenanceStatus, Substation, Threat, ThreatLevel

# ---------------------------------------------------------------------------
# Constants (POC values; see LLD §12 for the recalibration note)
# ---------------------------------------------------------------------------

HYPERSCALE_KEYWORDS: Tuple[str, ...] = (
    "amazon", "aws", "google", "microsoft", "azure", "meta", "facebook", "oracle cloud", "alibaba",
)
ENTERPRISE_KEYWORDS: Tuple[str, ...] = (
    "equinix", "digital realty", "cyrusone", "coresite", "qts", "ntt", "interxion", "telehouse",
    "colt", "data4", "vantage", "stack", "iron mountain",
)
LOAD_MW = {"hyperscale": 300.0, "enterprise": 50.0, "edge": 10.0}
DISTANCE_MULTIPLIER = {"hyperscale": 2.0, "enterprise": 1.5, "edge": 1.0}
CLASS_RANK = {"hyperscale": 0, "enterprise": 1, "edge": 2}

NEAR_KM = 3.2  # POC "within 2 miles"
DEFAULT_RADIUS_KM = 8.0  # POC 5 miles

PHYSICAL_WEIGHT = 0.55
FINANCIAL_WEIGHT = 0.45
BAND_CRITICAL = 80.0
BAND_HIGH = 60.0
BAND_MEDIUM = 40.0

EARTH_RADIUS_KM = 6371.0


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Single copy; the POC duplicated this in four files."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_facility(name: str, operator: str) -> Tuple[FacilityClass, float]:
    """Lower-cased substring match on operator or name -> (class, estimated load MW)."""
    haystack = f"{(operator or '').lower()} {(name or '').lower()}"
    if any(k in haystack for k in HYPERSCALE_KEYWORDS):
        return "hyperscale", LOAD_MW["hyperscale"]
    if any(k in haystack for k in ENTERPRISE_KEYWORDS):
        return "enterprise", LOAD_MW["enterprise"]
    return "edge", LOAD_MW["edge"]


def threat_level(facility_class: FacilityClass, distance_km: float, near_km: float = NEAR_KM) -> ThreatLevel:
    near = distance_km < near_km
    if facility_class == "hyperscale":
        return "critical" if near else "high"
    if facility_class == "enterprise":
        return "high" if near else "medium"
    return "medium" if near else "low"


def priority_key(threat: Threat) -> Tuple[int, float]:
    """Sort key for capping the threat list: class rank first, then distance."""
    return CLASS_RANK[threat.facility_class], threat.distance_km


def cap_threats(threats: Iterable[Threat], max_threats: int) -> List[Threat]:
    ordered = sorted(threats, key=priority_key)
    return ordered[: max(0, max_threats)]


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

def nearest_substation(lat: float, lon: float, substations: Sequence[Substation]) -> Optional[Substation]:
    if not substations:
        return None
    return min(substations, key=lambda s: haversine_km(lat, lon, s.lat, s.lon))


def shared_infrastructure(
    threats: Sequence[Threat],
    site_substation: Optional[Substation],
    substations: Sequence[Substation],
    near_km: float = NEAR_KM,
) -> Tuple[float, int, float]:
    """Return (shared_infra_score, shared_count, shared_mw).

    A threat shares the site's substation when its nearest substation has the same id. A threat
    whose nearest substation lies within ``near_km`` of the site's substation counts toward the
    proximity term (POC: +20 per such threat, capped at 20).
    """
    if site_substation is None or not threats:
        return 0.0, 0, 0.0
    shared_count = 0
    shared_mw = 0.0
    near_count = 0
    for t in threats:
        sub = nearest_substation(t.lat, t.lon, substations)
        if sub is None:
            continue
        if sub.id == site_substation.id:
            shared_count += 1
            shared_mw += t.estimated_load_mw
        elif haversine_km(sub.lat, sub.lon, site_substation.lat, site_substation.lon) < near_km:
            near_count += 1
    score = shared_infra_score(shared_count, shared_mw, near_count)
    return score, shared_count, shared_mw


def shared_infra_score(shared_count: int, shared_mw: float, near_substation_count: int) -> float:
    score = min(shared_count * 20.0, 40.0) + min(shared_mw / 200.0 * 40.0, 40.0) + min(20.0 * near_substation_count, 20.0)
    return round(min(score, 100.0), 1)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

def physical_score(threats: Sequence[Threat], radius_km: float, shared_infra: float) -> float:
    if not threats:
        return 0.0
    infra = 0.4 * shared_infra
    count = min(len(threats) * 4.0, 20.0)
    distance = 0.0
    for t in threats:
        weight = max(0.0, 1.0 - t.distance_km / radius_km) if radius_km > 0 else 0.0  # POC let this go negative
        distance += weight * 6.0 * DISTANCE_MULTIPLIER[t.facility_class]
    distance = min(distance, 25.0)
    capacity = min(total_capacity_mw(threats) / 50.0, 15.0)
    return round(min(infra + count + distance + capacity, 100.0), 1)


def total_capacity_mw(threats: Sequence[Threat]) -> float:
    return float(sum(t.estimated_load_mw for t in threats))


def infra_impact_pct_3yr(total_mw: float, shared_infra: float, threat_count: int) -> float:
    """Percent tariff pressure over three years from new load (rule of thumb: 100 MW ~ 1.5%)."""
    impact = (total_mw / 100.0 * 1.5) * (1.0 + shared_infra / 100.0) * (1.0 + min(threat_count, 20) / 40.0)
    return round(min(impact, 15.0), 2)


def annual_increase_pct(trend_pct_annual: Optional[float], infra_impact_3yr: float) -> float:
    return round((trend_pct_annual or 0.0) + infra_impact_3yr / 3.0, 2)


def financial_score(annual_incr_pct: float) -> float:
    return round(min(100.0, max(0.0, annual_incr_pct) / 15.0 * 100.0), 1)


def composite(physical: float, financial: Optional[float]) -> float:
    if financial is None:
        return round(physical, 1)
    return round(PHYSICAL_WEIGHT * physical + FINANCIAL_WEIGHT * financial, 1)


def band(score: float) -> Band:
    if score >= BAND_CRITICAL:
        return "critical"
    if score >= BAND_HIGH:
        return "high"
    if score >= BAND_MEDIUM:
        return "medium"
    return "low"


def coverage(tariff_status: ProvenanceStatus, narrative_available: bool) -> Coverage:
    tariff_ok = tariff_status in ("real", "cached")
    if tariff_ok and narrative_available:
        return "full"
    if tariff_ok:
        return "partial"
    return "physical_only"
