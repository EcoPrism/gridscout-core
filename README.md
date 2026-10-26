# gridscout-core

Score a street address for electricity-grid stress caused by nearby data-centre build-out.

Give it an address. It geocodes the site, lists the data-centre facilities within a radius
(PeeringDB), finds the serving substation and its neighbours (OpenStreetMap), pulls two years of
retail electricity prices for the state (US EIA), and turns that into a 0-100 score, a rate
projection and a cost exposure. Every section carries provenance: `real`, `cached` or
`unavailable`. Nothing is ever fabricated to fill a gap.

```
$ gridscout "21715 Filigree Ct, Ashburn, VA 20147" --country US --mwh 1000
  coordinates  done  {'lat': 39.0163342, 'lon': -77.4587688}
  datacenters  done  15
  substations  done  95
  tariff       done  VA commercial
  scoring      done  {'composite': 53.0, 'band': 'medium'}
  narrative    unavailable
  persist      done
21715 Filigree Ct, Ashburn, VA 20147  [US, US-VA]
composite 53.0 (medium), physical 80.0, shared infrastructure 50.0, financial 19.9, coverage partial
data centres within 8 km: 15 (~750 MW estimated)
   0.02 km  high     enterprise Equinix DC1-DC15,DC21-DC22 - Ashburn (Equinix, Inc.)
   0.36 km  high     enterprise Digital Realty IAD (44480 Hastings) (Digital Realty)
   ...
nearest substation: Nimbus Substation 230 kV, 0.62 km
tariff: 0.1114 USD/kWh (VA commercial), recent trend -2.02%
projection: +2.98%/yr, exposure 3,320 USD/yr on 1,000 MWh

provenance: coordinates=real, datacenters=real, substations=real, tariff=real, narrative=unavailable
```

Run on 26 October 2026 in about four seconds. The same address scored 85.9 (critical) on
28 September 2026 when the Virginia commercial tariff trend was +8.98 %; the financial half of
the score follows the latest EIA months, so it moves while the physical half stays put.

## Provenance of this code

This library was written in September 2026 inside a private EcoPrism codebase as the engine of
an unreleased "grid risk" module, itself a rewrite of a November 2025 proof of concept. It was
extracted into this standalone, Apache-2.0 repository on 26 October 2026. The extraction removed
the tenant-aware Azure Table storage, the hooks into EcoPrism's site and anomaly data, and a
Gemini-specific narrator, and replaced them with a JSON-file store, an address-only entry point and
a vendor-neutral `Narrator` interface. The scoring, providers, cache and prompts are otherwise the
September 2026 code.

The library is published ahead of the Meta AI Developer Hackathon (27 October to 4 November 2026)
so that work built on top of it during the event can be told apart from what existed before. Any
hackathon project that depends on this package should pin a tag and say so in its README. Commits
to this repository made during the event are dated and visible in the history.

## Install

```
pip install git+https://github.com/EcoPrism/gridscout-core.git
```

or for development:

```
git clone https://github.com/EcoPrism/gridscout-core.git
cd gridscout-core
pip install -e ".[dev]"
python -m pytest
```

Python 3.10 or newer.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EIA_API_KEY` | empty | Required for US tariffs. Free key at https://www.eia.gov/opendata/. Without it the assessment is `physical_only`. |
| `PEERINGDB_API_KEY` | empty | Optional. Raises the PeeringDB rate limit. |
| `GRIDSCOUT_RADIUS_KM` | `8` | Search radius for data centres. |
| `GRIDSCOUT_MAX_THREATS` | `15` | Facilities kept in the result, highest class first. |
| `GRIDSCOUT_FRESH_HOURS` | `168` | A stored full-coverage assessment younger than this is reused. |
| `GRIDSCOUT_DEFAULT_ANNUAL_MWH` | `120` | Consumption assumed for exposure when the caller gives none. The result is flagged. |
| `GRIDSCOUT_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Overpass endpoint. Some networks cannot reach the default; `https://overpass.openstreetmap.fr/api/interpreter` is a public alternative. |
| `GRIDSCOUT_NOMINATIM_USER_AGENT` | `gridscout-core` | Nominatim asks every application to identify itself. Set this to your project name. |
| `GRIDSCOUT_STORE_DIR` | `.gridscout` | Where the JSON file store writes. |

## Python API

```python
import asyncio
from gridscout import assess, site_from_address
from gridscout.store import JsonFileStore

async def main():
    site = site_from_address("21715 Filigree Ct, Ashburn, VA 20147", "US", annual_mwh=1000)

    async def emit(event, data):          # optional progress stream: run / stage / partial / complete
        print(event, data)

    result = await assess(site, emit=emit, store=JsonFileStore())
    print(result.scores.composite, result.scores.band, result.scores.coverage)
    for t in result.threats:
        print(t.distance_km, t.facility_class, t.name)

asyncio.run(main())
```

`assess` takes a `Providers` dataclass, so any stage can be swapped: a different geocoder, a
tariff provider for another country, a narrator. `SiteRef` accepts `lat`/`lon` directly when you
already have coordinates.

### Adding a narrator

The core ships no language-model client. Implement `gridscout.narrative.Narrator`:

```python
from gridscout.narrative import build_prompt, parse_narrative

class MyNarrator:
    name = "my-llm"

    async def generate(self, result):
        try:
            text = await my_model.complete(build_prompt(result), json_mode=True)
            return parse_narrative(text, model="my-model-id", country_code=result.site.country_code)
        except Exception:
            return None            # the assessment completes with coverage="partial"
```

`build_prompt` renders the structured result plus its provenance map and tells the model not to
speculate about unavailable sections. `parse_narrative` validates the JSON reply.

### Adding a tariff provider

Implement `fetch(site) -> ProviderResult[TariffSnapshot]`, set `countries`, and call
`gridscout.providers.tariffs.register(provider)`. Unknown countries fall back to a provider that
reports `unavailable`, and the assessment proceeds `physical_only`.

## Scoring

See [docs/scoring.md](docs/scoring.md) for the formulae. In short: facilities are classified by
operator keywords into hyperscale, enterprise or edge with an estimated load per class; a physical
score combines count, distance, capacity and whether facilities share the site's substation; a
financial score combines the recent tariff trend with a rule-of-thumb tariff pressure from the
new load; the composite is 0.55 physical plus 0.45 financial and banded into low, medium, high and
critical.

### Reference assessments

Recorded 28 September 2026 against live upstreams, with a narrator configured. Useful as a
smoke test that your environment reproduces the scores.

| Address | Composite | Band | Coverage | Facilities | Est. MW | Substation km | Tariff trend |
|---|---|---|---|---|---|---|---|
| 21715 Filigree Ct, Ashburn, VA 20147 | 85.9 | critical | full | 15 | 750 | 0.62 | +8.98 % |
| Boise, ID (city centre) | 28.5 | low | full | 2 | 20 | 0.77 | +5.73 % |
| Frankfurt-Ost, Germany | 83.2 | critical | physical_only | 15 | 470 | 0.15 | n/a |
| Rural South Dakota | 13.7 | low | full | 0 | 0 | 0.04 | +4.56 % |

Each assessment took 10 to 14 seconds, almost all of it waiting on upstream APIs.

### Known calibration gaps

These are inherited from the original design and are documented rather than hidden.

- **Capacity is summed after the cap.** `total_capacity_mw` counts only the facilities kept in
  the payload (`MAX_THREATS`), so a dense cluster is under-counted.
- **PeeringDB lists colocation and interconnection facilities, not hyperscale campuses.**
  An AWS, Microsoft or Google campus with no PeeringDB entry is invisible, so the `hyperscale`
  class rarely fires and the operators that matter most for grid load are the least covered.
- **The EIA trend is six months over the previous six**, not an annualised rate, but it feeds
  the `trend_pct_annual` field.
- **Loads are class constants** (300 / 50 / 10 MW), not measured or permitted capacity.
- **The 100 MW to 1.5 % tariff-pressure rule of thumb** is unvalidated against rate cases and
  ignores large-load tariffs that shield other customer classes.
- **US only for tariffs.** Other countries get a physical-only assessment.

## Data sources and their terms

You are responsible for complying with each upstream's terms. In summary, as of October 2026:

- **Nominatim (OpenStreetMap geocoding).** Usage policy: at most one request per second, a
  descriptive User-Agent, and no bulk geocoding. The provider enforces the spacing. Set
  `GRIDSCOUT_NOMINATIM_USER_AGENT`. Results are cached for 30 days.
- **Overpass API (OpenStreetMap substations).** OpenStreetMap data is ODbL; attribute
  "© OpenStreetMap contributors" wherever you display it. The public instances are shared and
  rate-limited. The provider holds a semaphore of two and caches for 24 hours.
- **PeeringDB.** The acceptable-use policy permits Internet research and analysis and prohibits
  bulk redistribution and commercial applications such as marketing lists. This library queries
  per location, caches per location for 24 hours and stores no bulk copy. The test fixture for
  PeeringDB is synthetic; it contains no PeeringDB data. Read https://www.peeringdb.com/aup
  before building a product on it.
- **US EIA Open Data.** Public domain. A free API key is required.

## What this is not

It does not model interconnection queues, utility cost allocation between customer classes,
large-load tariffs, or power quality. It does not know about data centres that are announced,
permitted or under construction unless they already appear in PeeringDB. It is a transparent
heuristic over public data with honest provenance, meant to be built on.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
