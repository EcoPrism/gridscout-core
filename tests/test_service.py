"""Pipeline tests with fake providers: event order, degradation, freshness and persistence.

Run: python -m pytest tests/test_service.py
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gridscout import service, settings
from gridscout.models import Narrative, Substation, TariffSnapshot, Threat
from gridscout.narrative import NullNarrator
from gridscout.providers import base
from gridscout.providers.geocode import GeocodeResult
from gridscout.store import JsonFileStore

ADDRESS = "1 Loudoun County Pkwy, Ashburn, VA 20147"


# -- fakes --------------------------------------------------------------------

class FakeFetch:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    async def fetch(self, *a, **k):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class FakeNarrator:
    name = "fake-llm"

    def __init__(self, narrative="ok"):
        self.narrative, self.calls = narrative, 0

    async def generate(self, result):
        self.calls += 1
        if self.narrative is None:
            return None
        return Narrative(synthesis="Six data centres share the substation.", recommendations=["PPA", "Storage", "Renegotiate"],
                         key_findings=["k1"], model="fake-model", region_prompt="us")


def _threats():
    return [
        Threat(id="pdb-1", name="Example Campus", operator="Amazon Example", facility_class="hyperscale", estimated_load_mw=300, distance_km=2.0, lat=39.03, lon=-77.48, threat_level="critical"),
        Threat(id="pdb-2", name="Example Colo", operator="Equinix Example", facility_class="enterprise", estimated_load_mw=50, distance_km=4.0, lat=39.05, lon=-77.46, threat_level="medium"),
    ]


def _subs():
    return [Substation(id="osm-way-1", name="Pleasant View", owner="Utility Co", voltage_kv=500, distance_km=1.9, lat=39.07, lon=-77.52),
            Substation(id="osm-way-2", name="Far", distance_km=9.0, lat=39.20, lon=-77.10)]


def _tariff_ok():
    return base.real("eia", TariffSnapshot(currency="USD", current_rate=0.1099, trend_pct_annual=4.2, region_label="VA commercial"))


def _providers(tariff=None, narrator=None, dc=None, geo=None):
    return service.Providers(
        geocoder=geo or FakeFetch(base.real("nominatim", GeocodeResult(39.0438, -77.4874, "US", "US-VA", "Ashburn"))),
        datacenters=dc or FakeFetch(base.real("peeringdb", _threats())),
        substations=FakeFetch(base.real("overpass", _subs())),
        tariff_for=lambda cc: FakeFetch(tariff if tariff is not None else _tariff_ok()),
        narrator=narrator or FakeNarrator(),
    )


async def _run(providers, store=None, site=None, **kw):
    events = []

    async def emit(event, data):
        events.append((event, data))

    site = site or service.site_from_address(ADDRESS, "US", annual_mwh=1000)
    result = await service.assess(site, emit=emit, providers=providers, store=store, **kw)
    return result, events


def _stages(events):
    return [(d["stage"], d["status"]) for e, d in events if e == "stage"]


# -- site keys ----------------------------------------------------------------

def test_site_from_address_is_stable_and_normalised():
    a = service.site_from_address(" 1 Main St, Town ", "us")
    b = service.site_from_address("1 MAIN ST, TOWN", "US")
    assert a.site_key == b.site_key and a.site_key.startswith("adhoc-") and a.country_code == "US" and a.is_adhoc
    assert service.site_from_address("2 Main St", "US").site_key != a.site_key


# -- happy path ---------------------------------------------------------------

async def test_full_coverage_event_order_and_result(tmp_path):
    store = JsonFileStore(tmp_path)
    result, events = await _run(_providers(), store)
    names = [e for e, _ in events]
    assert names[0] == "run" and names[-1] == "complete" and names.count("partial") == 3
    stages = _stages(events)
    for stage in ("coordinates", "datacenters", "substations", "tariff", "scoring", "narrative", "persist"):
        assert (stage, "start") in stages and (stage, "done") in stages
    assert stages.index(("scoring", "start")) > stages.index(("tariff", "done"))
    assert stages.index(("narrative", "start")) > stages.index(("scoring", "done"))
    assert stages.index(("persist", "start")) > stages.index(("narrative", "done"))

    assert result.scores.coverage == "full" and result.scores.financial is not None
    assert result.site.lat == 39.0438 and result.site.state_code == "US-VA" and result.site.is_adhoc
    assert result.projection.annual_exposure > 0 and result.projection.annual_mwh_is_default is False
    assert result.narrative.recommendations == ["PPA", "Storage", "Renegotiate"]
    assert {k: v.status for k, v in result.provenance.items()} == {
        "coordinates": "real", "datacenters": "real", "substations": "real", "tariff": "real", "narrative": "real"}
    assert result.provenance["narrative"].source == "fake-llm" and result.provenance["narrative"].note == "fake-model"
    partial_threats = next(d for e, d in events if e == "partial" and d["section"] == "threats")
    assert [t["id"] for t in partial_threats["data"]] == ["pdb-1", "pdb-2"]
    assert events[-1][1]["assessment_id"] == result.assessment_id

    stored = await store.get_assessment(result.assessment_id)
    assert stored is not None and stored.scores.composite == result.scores.composite


async def test_caller_coordinates_skip_geocoding():
    geo = FakeFetch(exc=RuntimeError("must not geocode"))
    site = service.site_from_address(ADDRESS, "US").model_copy(update={"lat": 39.0, "lon": -77.4, "state_code": "US-VA"})
    result, _ = await _run(_providers(geo=geo), site=site)
    assert geo.calls == 0 and result.provenance["coordinates"].source == "caller"


async def test_without_store_persist_is_skipped():
    result, events = await _run(_providers())
    assert ("persist", "skipped") in _stages(events) and result.scores.coverage == "full"


# -- degradation --------------------------------------------------------------

async def test_tariff_unavailable_is_physical_only():
    result, events = await _run(_providers(tariff=base.unavailable("tariff", "no tariff provider for DE")),
                                site=service.site_from_address("Hanauer Landstr. 1, Frankfurt", "DE"))
    s = result.scores
    assert s.coverage == "physical_only" and s.financial is None and s.composite == s.physical
    assert result.projection is None and result.tariff is None
    assert result.provenance["tariff"].status == "unavailable"
    assert ("tariff", "unavailable") in _stages(events)


async def test_narrative_missing_is_partial():
    result, events = await _run(_providers(narrator=FakeNarrator(None)))
    assert result.narrative is None and result.scores.coverage == "partial"
    assert result.provenance["narrative"].status == "unavailable"
    assert ("narrative", "unavailable") in _stages(events)


async def test_default_narrator_is_null():
    result, _ = await _run(_providers(narrator=NullNarrator()))
    assert result.narrative is None and result.provenance["narrative"].source == "none"


async def test_datacenters_unavailable_scores_zero_physical():
    result, events = await _run(_providers(dc=FakeFetch(base.unavailable("peeringdb", "HTTP 503 after 3 attempts"))))
    assert result.threats == [] and result.scores.physical == 0.0 and result.total_capacity_mw == 0.0
    assert ("datacenters", "unavailable") in _stages(events)


async def test_default_mwh_flagged_when_absent():
    result, _ = await _run(_providers(), site=service.site_from_address(ADDRESS, "US"))
    assert result.projection.annual_mwh_is_default is True and result.projection.assumed_annual_mwh == settings.DEFAULT_ANNUAL_MWH


async def test_geocode_failure_raises_assessment_error():
    events = []

    async def emit(event, data):
        events.append((event, data))

    providers = _providers(geo=FakeFetch(base.unavailable("nominatim", "address not found")))
    with pytest.raises(service.AssessmentError, match="address not found"):
        await service.assess(service.site_from_address(ADDRESS, "US"), emit=emit, providers=providers)
    assert ("coordinates", "unavailable") in _stages(events) and not any(e == "complete" for e, _ in events)


async def test_unexpected_provider_exception_propagates():
    with pytest.raises(RuntimeError, match="boom"):
        await _run(_providers(dc=FakeFetch(exc=RuntimeError("boom"))))


async def test_store_failure_does_not_lose_the_result(tmp_path):
    class BrokenStore(JsonFileStore):
        async def save_assessment(self, result):
            raise OSError("disk full")

    result, events = await _run(_providers(), BrokenStore(tmp_path))
    assert result.scores.coverage == "full"
    assert ("persist", "unavailable") in _stages(events)


# -- freshness ----------------------------------------------------------------

async def test_fresh_assessment_short_circuits_and_force_reruns(tmp_path):
    store = JsonFileStore(tmp_path)
    first, _ = await _run(_providers(), store)
    failing = _providers(dc=FakeFetch(exc=RuntimeError("must not be called")))
    cached, events = await _run(failing, store)
    assert [e for e, _ in events] == ["run", "stage", "complete"]
    assert events[1][1] == {"stage": "cached", "status": "done", "detail": first.assessment_id}
    assert cached.assessment_id == first.assessment_id
    with pytest.raises(RuntimeError):
        await _run(failing, store, force=True)  # force bypassed the cache and hit the failing provider


async def test_partial_or_stale_latest_does_not_short_circuit(tmp_path):
    store = JsonFileStore(tmp_path)
    partial, _ = await _run(_providers(narrator=FakeNarrator(None)), store)
    assert partial.scores.coverage == "partial"
    again, _ = await _run(_providers(), store)
    assert again.assessment_id != partial.assessment_id  # partial never counts as fresh
    with patch.object(settings, "FRESH_HOURS", 0):
        third, _ = await _run(_providers(), store)
    assert third.assessment_id != again.assessment_id  # older than FRESH_HOURS


def test_fresh_helper():
    now = datetime.now(timezone.utc).isoformat()
    assert service._fresh({"coverage": "full", "created_at": now}, 168) is True
    assert service._fresh({"coverage": "partial", "created_at": now}, 168) is False
    assert service._fresh({"coverage": "full", "created_at": "not-a-date"}, 168) is False
    assert service._fresh(None, 168) is False
