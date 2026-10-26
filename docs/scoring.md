# Scoring

All functions live in `gridscout/scoring.py` and `gridscout/financial.py`. They are pure: no I/O,
no settings reads at call time. Constants are module-level so a host can override them.

## Facility classification

PeeringDB returns facility name and operator. A lower-cased substring match assigns a class and
an **estimated** load. These are class constants, not measured loads.

| Class | Match (operator or name) | Estimated load |
|---|---|---|
| hyperscale | amazon, aws, google, microsoft, azure, meta, facebook, oracle cloud, alibaba | 300 MW |
| enterprise | equinix, digital realty, cyrusone, coresite, qts, ntt, interxion, telehouse, colt, data4, vantage, stack, iron mountain | 50 MW |
| edge | anything else | 10 MW |

Distances are km. The default radius is 8 km (about 5 miles). "Near" is 3.2 km (about 2 miles).

## Formulae

```
threat_level  = critical if hyperscale & d<3.2 · high if hyperscale or (enterprise & d<3.2)
                · medium if enterprise or (edge & d<3.2) · low otherwise
shared_infra  = min(shared_count×20, 40) + min(shared_mw/200×40, 40)
                + min(20 × n_facility_substations_within_3.2km_of_site_substation, 20)
physical      = 0.4×shared_infra + min(n×4, 20) + min(Σ (1−d/r)×6×mult, 25) + min(total_mw/50, 15)
                mult: hyperscale 2.0 / enterprise 1.5 / edge 1.0
infra_impact  = min( (total_mw/100 × 1.5) × (1 + shared_infra/100) × (1 + min(n,20)/40), 15 )   % over 3 years
annual_incr   = (tariff.trend_pct_annual or 0) + infra_impact/3
financial     = min(100, annual_incr/15 × 100)                 None when the tariff is unavailable
composite     = 0.55×physical + 0.45×financial                 or physical alone when financial is None
band          = critical ≥80 · high ≥60 · medium ≥40 · low
coverage      = full (tariff ok & narrative) · partial (tariff ok, no narrative) · physical_only (no tariff)
exposure      = annual_mwh×1000 × current_rate × annual_incr/100   currency/year
                (annual_mwh defaults to 120 when unknown and the result is flagged)
```

`RateProjection.monthly` holds 12 points `current_rate × (1 + annual_incr/100 × m/12)`.

A facility "shares" the site's substation when the OpenStreetMap substation nearest to the
facility is the same object as the one nearest to the site. This is a proximity heuristic, not a
network topology lookup.

## What the numbers are not

- Loads are class estimates. A hyperscale campus may draw far more or far less than 300 MW.
- `trend_pct_annual` from EIA is the latest six months over the previous six, not an annualised rate.
- The 100 MW ≈ 1.5 % tariff-pressure rule of thumb has not been calibrated against rate cases.
- Nothing here models interconnection queues, large-load tariffs, or how a utility allocates
  costs between customer classes.
