"""Provider tests with recorded or synthetic fixtures and respx-mocked HTTP.

Run: python -m pytest tests/test_providers.py
"""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from tenacity import wait_none

from gridscout import cache, scoring, settings
from gridscout.models import SeriesPoint, SiteRef
from gridscout.providers import base, datacenters, geocode, substations
from gridscout.providers.tariffs import NullTariffProvider, USEIATariffProvider, provider_for, us_eia

FIXTURES = Path(__file__).parent / "fixtures"
ASHBURN = (39.0438, -77.4874)


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fast_retries():
    with patch.object(base, "RETRY_WAIT", wait_none()):
        yield


@pytest.fixture(autouse=True)
def _empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
async def _fresh_client():
    await base.aclose_http_client()
    yield
    await base.aclose_http_client()


# -- base: retry policy -------------------------------------------------------

@respx.mock
async def test_request_json_retries_429_then_succeeds():
    route = respx.get("https://example.test/x").mock(side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": 1})])
    assert await base.request_json("GET", "https://example.test/x") == {"ok": 1}
    assert route.call_count == 2


@respx.mock
async def test_request_json_gives_up_after_three_429():
    route = respx.get("https://example.test/x").mock(return_value=httpx.Response(429))
    with pytest.raises(base.RetryableStatus):
        await base.request_json("GET", "https://example.test/x")
    assert route.call_count == 3


@respx.mock
async def test_request_json_does_not_retry_404():
    route = respx.get("https://example.test/x").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await base.request_json("GET", "https://example.test/x")
    assert route.call_count == 1


def test_failure_note_wording():
    assert base.failure_note(base.RetryableStatus(429, "u")) == "HTTP 429 after 3 attempts"
    assert "ConnectError" in base.failure_note(httpx.ConnectError("boom"))


def test_user_agent_identifies_the_project():
    assert base.get_http_client().headers["User-Agent"].startswith("gridscout-core/")


# -- PeeringDB ----------------------------------------------------------------

@respx.mock
async def test_peeringdb_parses_fixture_within_radius():
    fixture = _load("peeringdb_fac_synthetic.json")
    route = respx.get(datacenters.PEERINGDB_FAC_URL).mock(return_value=httpx.Response(200, json=fixture))
    with patch.multiple(settings, MAX_THREATS=100, PEERINGDB_API_KEY=""):
        res = await datacenters.PeeringDBProvider().fetch(*ASHBURN, radius_km=8.0)
    assert res.ok and res.provenance.status == "real" and res.provenance.source == "peeringdb"
    assert route.called and "Authorization" not in route.calls.last.request.headers
    threats = res.data
    assert len(threats) == 7  # 9 rows: one beyond 8 km, one without coordinates
    assert all(t.distance_km <= 8.0 for t in threats)
    assert threats == sorted(threats, key=scoring.priority_key)  # class rank, then distance
    assert {t.facility_class for t in threats} == {"hyperscale", "enterprise", "edge"}
    assert all(t.id.startswith("pdb-") for t in threats)


@respx.mock
async def test_peeringdb_caps_and_sends_api_key():
    fixture = _load("peeringdb_fac_synthetic.json")
    route = respx.get(datacenters.PEERINGDB_FAC_URL).mock(return_value=httpx.Response(200, json=fixture))
    with patch.multiple(settings, MAX_THREATS=3, PEERINGDB_API_KEY="secret"):
        res = await datacenters.PeeringDBProvider().fetch(*ASHBURN, radius_km=8.0)
    assert len(res.data) == 3
    assert route.calls.last.request.headers["Authorization"] == "Api-Key secret"
    ranks = [{"hyperscale": 0, "enterprise": 1, "edge": 2}[t.facility_class] for t in res.data]
    assert ranks == sorted(ranks)  # the cap keeps the highest-priority class first


@respx.mock
async def test_peeringdb_unavailable_after_retries():
    route = respx.get(datacenters.PEERINGDB_FAC_URL).mock(return_value=httpx.Response(503))
    res = await datacenters.PeeringDBProvider().fetch(*ASHBURN, radius_km=8.0)
    assert res.data is None and res.provenance.status == "unavailable"
    assert res.provenance.note == "HTTP 503 after 3 attempts"
    assert route.call_count == 3


# -- Overpass -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("138000;230000", 230), ("500000;230000;34500;13200", 500), ("230000", 230), ("115", 115), ("", None), (None, None), ("abc", None),
])
def test_parse_voltage(raw, expected):
    assert substations.parse_voltage(raw) == expected


def test_query_radius_and_query_text():
    assert substations.query_radius_km(8.0) == 16.0 and substations.query_radius_km(12.0) == 24.0
    q = substations.build_query(*ASHBURN, 16.0)
    assert 'way["power"="substation"](around:16000,39.0438,-77.4874)' in q and "out center tags" in q


@respx.mock
async def test_overpass_parses_fixture_nearest_first():
    fixture = _load("overpass_substations_ashburn.json")
    route = respx.post(settings.OVERPASS_URL).mock(return_value=httpx.Response(200, json=fixture))
    res = await substations.OverpassProvider().fetch(*ASHBURN, radius_km=8.0)
    assert res.ok and res.provenance.source == "overpass"
    subs = res.data
    assert len(subs) == len(fixture["elements"])
    assert subs == sorted(subs, key=lambda s: s.distance_km)
    assert subs[0].id.startswith("osm-way-") and subs[0].name
    assert any(s.voltage_kv and s.voltage_kv >= 230 for s in subs)
    assert route.calls.last.request.content  # query posted as form body


@respx.mock
async def test_overpass_unavailable_on_transport_error():
    respx.post(settings.OVERPASS_URL).mock(side_effect=httpx.ConnectError("down"))
    res = await substations.OverpassProvider().fetch(*ASHBURN)
    assert res.provenance.status == "unavailable" and "ConnectError" in res.provenance.note


# -- EIA tariff ---------------------------------------------------------------

def _site(state="US-VA", cc="US"):
    return SiteRef(site_key="k", name="n", address="a", country_code=cc, state_code=state, is_adhoc=True)


@respx.mock
async def test_eia_sends_state_and_sector_facets_and_parses():
    fixture = _load("eia_retail_sales_va_com.json")
    route = respx.get(us_eia.EIA_URL).mock(return_value=httpx.Response(200, json=fixture))
    with patch.object(settings, "EIA_API_KEY", "k"):
        res = await USEIATariffProvider().fetch(_site())
    params = dict(route.calls.last.request.url.params)
    assert params["facets[stateid][]"] == "VA" and params["facets[sectorid][]"] == "COM" and params["api_key"] == "k"
    t = res.data
    assert res.ok and t.currency == "USD" and t.region_label == "VA commercial"
    assert t.current_rate == pytest.approx(0.1099)  # 10.99 c/kWh newest row
    assert len(t.series) == 24 and t.series[0].period == "2026-06" and t.series[0].period > t.series[-1].period
    assert t.trend_pct_annual is not None


@respx.mock
async def test_eia_residential_sector_is_a_constructor_choice():
    fixture = _load("eia_retail_sales_va_com.json")
    route = respx.get(us_eia.EIA_URL).mock(return_value=httpx.Response(200, json=fixture))
    with patch.object(settings, "EIA_API_KEY", "k"):
        res = await USEIATariffProvider(sector="RES").fetch(_site())
    assert dict(route.calls.last.request.url.params)["facets[sectorid][]"] == "RES"
    assert res.data.region_label == "VA residential"


def test_eia_trend_needs_twelve_points():
    pts = [SeriesPoint(period=f"2026-{m:02d}", value=0.11) for m in range(6, 0, -1)]
    assert us_eia.compute_trend(pts) is None
    pts = [SeriesPoint(period=str(i), value=0.12) for i in range(6)] + [SeriesPoint(period=str(i), value=0.10) for i in range(6)]
    assert us_eia.compute_trend(pts) == pytest.approx(20.0)


@pytest.mark.parametrize("state,expected", [("US-VA", "VA"), ("va", "VA"), (None, None), ("", None)])
def test_eia_state_from_site(state, expected):
    assert us_eia.state_from_site(_site(state=state)) == expected


async def test_eia_unavailable_without_state_or_key():
    with patch.object(settings, "EIA_API_KEY", "k"):
        res = await USEIATariffProvider().fetch(_site(state=None))
    assert res.provenance.status == "unavailable" and res.provenance.note == "no state"
    with patch.object(settings, "EIA_API_KEY", ""):
        res = await USEIATariffProvider().fetch(_site())
    assert res.provenance.note == "EIA_API_KEY not configured"


async def test_registry_falls_back_to_null():
    assert isinstance(provider_for("US"), USEIATariffProvider)
    assert isinstance(provider_for("DE"), NullTariffProvider)
    res = await provider_for("DE").fetch(_site(state="DE-HE", cc="DE"))
    assert res.provenance.status == "unavailable" and "DE" in res.provenance.note


# -- Geocoder -----------------------------------------------------------------

_RAW = {"display_name": "Hanauer Landstrasse 1, Frankfurt, 60314, Deutschland",
        "address": {"country_code": "de", "ISO3166-2-lvl4": "DE-HE", "city": "Frankfurt"}}


async def test_geocoder_parses_and_paces():
    with patch.object(geocode, "_geocode_sync", return_value={"lat": 50.11, "lon": 8.70, "raw": _RAW}), patch.object(geocode, "MIN_SPACING_S", 0):
        res = await geocode.NominatimGeocoder().fetch("Hanauer Landstr. 1, Frankfurt", "DE")
    assert res.ok and res.data.country_code == "DE" and res.data.state_code == "DE-HE"
    assert res.data.lat == 50.11


async def test_geocoder_not_found_and_error():
    with patch.object(geocode, "_geocode_sync", return_value=None), patch.object(geocode, "MIN_SPACING_S", 0):
        res = await geocode.NominatimGeocoder().fetch("nowhere", None)
    assert res.provenance.status == "unavailable" and res.provenance.note == "address not found"
    with patch.object(geocode, "_geocode_sync", side_effect=RuntimeError("geopy down")), patch.object(geocode, "MIN_SPACING_S", 0):
        res = await geocode.NominatimGeocoder().fetch("x", None)
    assert res.provenance.status == "unavailable" and "RuntimeError" in res.provenance.note


# -- cache --------------------------------------------------------------------

async def test_cache_noop_when_disabled():
    with patch.object(cache, "_backend", None):
        await cache.set("k", {"a": 1}, 10)
        assert await cache.get("k") is None


async def test_memory_cache_expires():
    mem = cache.MemoryCache()
    await mem.set("k", "v", 10)
    assert await mem.get("k") == "v"
    mem._items["k"] = (0.0, "v")  # already expired
    assert await mem.get("k") is None


@respx.mock
async def test_cache_hit_reports_cached_provenance():
    fixture = _load("peeringdb_fac_synthetic.json")
    route = respx.get(datacenters.PEERINGDB_FAC_URL).mock(return_value=httpx.Response(200, json=fixture))
    first = await datacenters.PeeringDBProvider().fetch(38.5, -77.0, radius_km=8.0)
    second = await datacenters.PeeringDBProvider().fetch(38.5, -77.0, radius_km=8.0)
    assert first.provenance.status == "real" and second.provenance.status == "cached"
    assert route.call_count == 1
    assert [t.id for t in second.data] == [t.id for t in first.data]
