"""JsonFileStore round-trips, index maintenance and history ordering."""

from datetime import datetime, timedelta, timezone

from gridscout.models import AssessmentResult, Provenance, SectionScores, SiteRef, Threat
from gridscout.store import HISTORY_LIMIT, JsonFileStore, summary


def _result(assessment_id, site_key="adhoc-1", composite=42.0, minutes_ago=0):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return AssessmentResult(
        assessment_id=assessment_id,
        site=SiteRef(site_key=site_key, name="Site", address="1 Main St", country_code="US", state_code="US-VA", lat=39.0, lon=-77.4, is_adhoc=True),
        radius_km=8.0,
        threats=[Threat(id="pdb-1", name="X", operator="Equinix", facility_class="enterprise", estimated_load_mw=50, distance_km=1.0, lat=39.0, lon=-77.4, threat_level="high")],
        total_capacity_mw=50.0,
        scores=SectionScores(physical=30.0, shared_infra=0.0, financial=None, composite=composite, band="medium", coverage="physical_only"),
        provenance={"coordinates": Provenance(source="caller", status="real")},
        created_at=created,
        duration_ms=5,
    )


async def test_save_and_read_back(tmp_path):
    store = JsonFileStore(tmp_path)
    await store.save_assessment(_result("a1"))
    loaded = await store.get_assessment("a1")
    assert loaded is not None and loaded.assessment_id == "a1" and loaded.threats[0].operator == "Equinix"
    assert await store.get_assessment("missing") is None
    assert (tmp_path / "assessments" / "a1.json").exists() and (tmp_path / "index.json").exists()


async def test_latest_and_history_newest_first(tmp_path):
    store = JsonFileStore(tmp_path)
    await store.save_assessment(_result("a1", composite=10.0, minutes_ago=30))
    await store.save_assessment(_result("a2", composite=55.0))
    await store.save_assessment(_result("b1", site_key="adhoc-2"))
    latest = await store.latest_for_site("adhoc-1")
    assert latest["assessment_id"] == "a2" and latest["composite"] == 55.0
    assert await store.latest_for_site("nope") is None
    assert {r["site_key"] for r in await store.list_latest()} == {"adhoc-1", "adhoc-2"}
    hist = await store.history("adhoc-1")
    assert [r["assessment_id"] for r in hist] == ["a2", "a1"]
    assert await store.history("adhoc-1", limit=1) == hist[:1]
    assert await store.history("nope") == []


async def test_history_is_capped(tmp_path):
    store = JsonFileStore(tmp_path)
    for i in range(HISTORY_LIMIT + 5):
        await store.save_assessment(_result(f"a{i}"))
    hist = await store.history("adhoc-1", limit=1000)
    assert len(hist) == HISTORY_LIMIT and hist[0]["assessment_id"] == f"a{HISTORY_LIMIT + 4}"


def test_summary_columns_are_scalar():
    row = summary(_result("a1"))
    assert row["datacenter_count"] == 1 and row["financial"] is None and row["currency"] is None
    assert all(not isinstance(v, (dict, list)) for v in row.values())


def test_assessment_path_is_sanitised(tmp_path):
    store = JsonFileStore(tmp_path)
    assert store._assessment_path("../x/..\\y").parent == tmp_path / "assessments"
