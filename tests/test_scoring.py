"""Unit tests for GridScout pure scoring and financial functions (LLD §5.6, §11).

Run: python -m pytest tests/test_scoring.py
"""

from datetime import date

import pytest

from gridscout import financial, scoring
from gridscout.models import Substation, TariffSnapshot, Threat


def _threat(id_, cls, d_km, lat=39.0, lon=-77.4):
    return Threat(
        id=id_, name=id_, operator=id_, facility_class=cls, estimated_load_mw=scoring.LOAD_MW[cls],
        distance_km=d_km, lat=lat, lon=lon, threat_level=scoring.threat_level(cls, d_km),
    )


# ── geometry ─────────────────────────────────────────────────────────────────

def test_haversine_known_pair():
    # Frankfurt Hbf -> Frankfurt Airport, about 10.5 km great-circle for these coordinates
    d = scoring.haversine_km(50.1070, 8.6630, 50.0379, 8.5622)
    assert 10.3 < d < 10.7


def test_haversine_zero():
    assert scoring.haversine_km(1.0, 2.0, 1.0, 2.0) == 0.0


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,operator,expected", [
    ("US-EAST-1", "Amazon Web Services", ("hyperscale", 300.0)),
    ("FRA16", "Interxion", ("enterprise", 50.0)),
    ("Digital Realty Ashburn", "DLR", ("enterprise", 50.0)),
    ("Some Colo", "Local ISP", ("edge", 10.0)),
    ("", "", ("edge", 10.0)),
])
def test_classify_facility(name, operator, expected):
    assert scoring.classify_facility(name, operator) == expected


@pytest.mark.parametrize("cls,d,expected", [
    ("hyperscale", 1.0, "critical"), ("hyperscale", 5.0, "high"),
    ("enterprise", 1.0, "high"), ("enterprise", 5.0, "medium"),
    ("edge", 1.0, "medium"), ("edge", 5.0, "low"),
    ("hyperscale", 3.2, "high"),  # boundary is strict <
])
def test_threat_level(cls, d, expected):
    assert scoring.threat_level(cls, d) == expected


def test_cap_threats_orders_by_class_then_distance():
    ts = [_threat("e-far", "edge", 7), _threat("h-far", "hyperscale", 6), _threat("h-near", "hyperscale", 1), _threat("ent", "enterprise", 2)]
    capped = scoring.cap_threats(ts, 3)
    assert [t.id for t in capped] == ["h-near", "h-far", "ent"]


# ── shared infrastructure ────────────────────────────────────────────────────

def test_shared_infra_score_components_and_caps():
    assert scoring.shared_infra_score(0, 0, 0) == 0.0
    assert scoring.shared_infra_score(1, 50, 0) == 20.0 + 10.0
    assert scoring.shared_infra_score(5, 1000, 3) == 40.0 + 40.0 + 20.0  # every term capped


def test_shared_infrastructure_geometry():
    site_sub = Substation(id="s1", name="Site sub", distance_km=1.0, lat=39.00, lon=-77.40)
    far_sub = Substation(id="s2", name="Far sub", distance_km=9.0, lat=39.20, lon=-77.10)
    near_sub = Substation(id="s3", name="Near sub", distance_km=2.0, lat=39.01, lon=-77.41)  # ~1.4 km from s1
    t_shared = _threat("aws", "hyperscale", 2.0, lat=39.001, lon=-77.401)  # nearest = s1
    t_near = _threat("eqx", "enterprise", 2.5, lat=39.011, lon=-77.411)  # nearest = s3 (near s1)
    t_far = _threat("edge", "edge", 7.0, lat=39.19, lon=-77.11)  # nearest = s2
    score, count, mw = scoring.shared_infrastructure([t_shared, t_near, t_far], site_sub, [site_sub, far_sub, near_sub])
    assert count == 1 and mw == 300.0
    assert score == scoring.shared_infra_score(1, 300.0, 1) == 20.0 + 40.0 + 20.0


def test_shared_infrastructure_without_site_substation():
    assert scoring.shared_infrastructure([_threat("a", "edge", 1)], None, []) == (0.0, 0, 0.0)


# ── physical score ───────────────────────────────────────────────────────────

def test_physical_score_empty():
    assert scoring.physical_score([], 8.0, 50.0) == 0.0


def test_physical_score_hand_computed():
    ts = [_threat("h", "hyperscale", 2.0), _threat("e", "enterprise", 4.0)]
    # infra 0.4*30 = 12 ; count 2*4 = 8 ; distance (1-2/8)*6*2 + (1-4/8)*6*1.5 = 9 + 4.5 = 13.5 ; capacity 350/50 = 7
    assert scoring.physical_score(ts, 8.0, 30.0) == pytest.approx(40.5)


def test_physical_score_weight_floored_at_zero():
    beyond = _threat("x", "hyperscale", 12.0)  # outside radius: POC gave a negative contribution
    inside = _threat("y", "hyperscale", 12.0)
    assert scoring.physical_score([beyond], 8.0, 0.0) == scoring.physical_score([inside], 8.0, 0.0) == pytest.approx(4 + 0 + 6.0)


def test_physical_score_saturates_like_poc_ashburn():
    # LLD §11 worked example: 41 facilities, 2 847 MW, shared 72 -> count and capacity terms saturate
    ts = [_threat(f"h{i}", "hyperscale", 7.9) for i in range(9)] + [_threat(f"e{i}", "enterprise", 7.9) for i in range(32)]
    assert len(ts) == 41 and scoring.total_capacity_mw(ts) == 9 * 300 + 32 * 50  # 4300 MW with these classes
    score = scoring.physical_score(ts, 8.0, 72.0)
    assert score >= 0.4 * 72 + 20 + 15  # 63.8 floor from saturated terms
    assert score <= 100.0


# ── financial terms ──────────────────────────────────────────────────────────

def test_infra_impact_matches_worked_example():
    # 2 847 MW, shared 72, 41 facilities -> capped at 15 % over 3 years
    assert scoring.infra_impact_pct_3yr(2847, 72, 41) == 15.0
    # small site: 60 MW, shared 0, 3 facilities -> 0.9 * 1.0 * 1.075 = 0.9675
    assert scoring.infra_impact_pct_3yr(60, 0, 3) == pytest.approx(0.97, abs=0.01)


def test_annual_increase_pct_worked_example():
    # trend 7.1 %/yr + 15/3 = 12.1 % (LLD §11)
    assert scoring.annual_increase_pct(7.1, 15.0) == 12.1
    assert scoring.annual_increase_pct(None, 3.0) == 1.0


@pytest.mark.parametrize("incr,expected", [(0, 0.0), (7.5, 50.0), (15, 100.0), (30, 100.0), (-2, 0.0)])
def test_financial_score(incr, expected):
    assert scoring.financial_score(incr) == expected


def test_composite_with_and_without_financial():
    assert scoring.composite(60, 40) == pytest.approx(0.55 * 60 + 0.45 * 40)
    assert scoring.composite(63.8, None) == 63.8


@pytest.mark.parametrize("score,expected", [
    (100, "critical"), (80, "critical"), (79.9, "high"), (60, "high"), (59.9, "medium"), (40, "medium"), (39.9, "low"), (0, "low"),
])
def test_band_thresholds(score, expected):
    assert scoring.band(score) == expected


@pytest.mark.parametrize("tariff,narr,expected", [
    ("real", True, "full"), ("cached", True, "full"),
    ("real", False, "partial"), ("cached", False, "partial"),
    ("unavailable", True, "physical_only"), ("estimated", True, "physical_only"), ("unavailable", False, "physical_only"),
])
def test_coverage_matrix(tariff, narr, expected):
    assert scoring.coverage(tariff, narr) == expected


# ── financial.py ─────────────────────────────────────────────────────────────

def _tariff(rate=0.10, trend=5.0):
    return TariffSnapshot(currency="USD", current_rate=rate, trend_pct_annual=trend, region_label="VA commercial")


def test_rate_projection_points_and_exposure():
    p = financial.rate_projection(_tariff(), annual_increase_pct=12.0, infra_impact_pct_3yr=15.0, annual_mwh=1000.0, start=date(2026, 9, 1))
    assert len(p.monthly) == 12
    assert p.monthly[0].period == "2026-10" and p.monthly[-1].period == "2027-09"
    assert p.monthly[-1].value == pytest.approx(0.10 * 1.12)
    assert p.year_end_rate == pytest.approx(0.112)
    assert p.annual_exposure == pytest.approx(1000 * 1000 * 0.10 * 0.12)  # 12 000 USD/yr
    assert p.assumed_annual_mwh == 1000.0 and p.annual_mwh_is_default is False


def test_rate_projection_default_mwh_flagged():
    p = financial.rate_projection(_tariff(), 10.0, 3.0, annual_mwh=None)
    assert p.annual_mwh_is_default is True
    assert p.assumed_annual_mwh == financial.DEFAULT_ANNUAL_MWH
    assert p.annual_exposure == pytest.approx(120 * 1000 * 0.10 * 0.10)


def test_cost_5yr_compounds():
    flat = financial.cost_5yr(0.10, 0.0, 100.0)
    assert flat == pytest.approx(5 * 100 * 1000 * 0.10)
    rising = financial.cost_5yr(0.10, 10.0, 100.0)
    assert rising == pytest.approx(10000 * sum(1.1 ** y for y in range(5)))
