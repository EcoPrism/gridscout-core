"""Prompt fragments for the GridScout narrative (LLD §5.5).

Prompt = PERSONA + region fragment + data block rendered from the structured AssessmentResult.
The data block carries the provenance map so the model is told which sections are unavailable
and instructed not to speculate about them.
"""

from __future__ import annotations

from typing import Any, Dict

PERSONA = """You are a strategic energy-risk analyst advising a company's sustainability and facilities team.
You analyse how nearby data-centre growth strains the local electricity grid and what that means for
the site's power cost and supply. Write for a decision-maker: concrete, quantified, no filler.

Rules:
- Use only the numbers in the DATA block. Do not invent facilities, tariffs, dates or percentages.
- A section marked "unavailable" in PROVENANCE has no data. Say so where relevant and do not guess its values.
- Explain the mechanism (data centres -> substation load -> network upgrades -> tariff pass-through), not just the facts.
- Recommendations must be specific to this site and market, actionable within 24 months, and ordered by value.
"""

REGION_FRAGMENTS: Dict[str, str] = {
    "us": (
        "Market context: United States. Tariffs are set through utility rate cases before state commissions; "
        "transmission and distribution upgrade costs are recovered from ratepayers. Relevant levers: fixed-price "
        "supply contracts, virtual or physical PPAs, ISO/RTO demand-response programmes (PJM, ERCOT, CAISO), "
        "battery storage against demand charges, interconnection-queue monitoring."
    ),
    "eu": (
        "Market context: European Union / EEA. Network tariffs are regulated nationally and pass grid-connection "
        "and reinforcement costs to consumers; wholesale exposure depends on the supply contract. Relevant levers: "
        "fixed-price or indexed supply renegotiation, PPAs and guarantees of origin, flexibility and demand-response "
        "markets via the TSO, on-site generation and storage, monitoring of grid-connection queues and ENTSO-E adequacy signals."
    ),
    "in": (
        "Market context: India. Tariffs are set by state electricity regulatory commissions through DISCOM tariff "
        "orders; cross-subsidy surcharges and open-access rules matter for large consumers. Relevant levers: open-access "
        "renewable procurement, captive or group-captive solar, time-of-day tariff optimisation, battery storage for "
        "reliability, tracking of DISCOM tariff petitions."
    ),
    "generic": (
        "Market context: use general principles. Grid reinforcement costs driven by large new loads are typically "
        "recovered through regulated network tariffs. Relevant levers: fixed-price supply contracts, renewable PPAs, "
        "on-site generation and storage, demand flexibility."
    ),
}

EU_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
    "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "NO", "LI", "CH", "GB", "UK",
})


def region_for(country_code: str) -> str:
    cc = (country_code or "").upper()
    if cc == "US":
        return "us"
    if cc in EU_COUNTRIES:
        return "eu"
    if cc == "IN":
        return "in"
    return "generic"


def render_data_block(result: Any) -> str:
    """Compact, deterministic text rendering of an AssessmentResult for the prompt."""
    site = result.site
    lines = ["DATA", f"Site: {site.name} — {site.address} ({site.country_code}{', ' + site.state_code if site.state_code else ''})"]
    if site.annual_mwh:
        lines.append(f"Annual electricity use: {site.annual_mwh:,.0f} MWh")
    if site.supplier_name:
        lines.append(f"Electricity supplier: {site.supplier_name}")
    lines.append(f"Search radius: {result.radius_km:g} km")

    lines.append(f"Data centres within radius: {len(result.threats)} (estimated {result.total_capacity_mw:,.0f} MW)")
    for t in result.threats[:10]:
        lines.append(f"  - {t.name} ({t.operator}), {t.facility_class}, ~{t.estimated_load_mw:g} MW, {t.distance_km} km, threat {t.threat_level}")
    if len(result.threats) > 10:
        lines.append(f"  - ... and {len(result.threats) - 10} more")

    if result.substation:
        s = result.substation
        lines.append(f"Nearest substation: {s.name}{f' ({s.voltage_kv} kV)' if s.voltage_kv else ''}{f', operator {s.owner}' if s.owner else ''}, {s.distance_km} km")
    else:
        lines.append("Nearest substation: unavailable")

    if result.tariff:
        t = result.tariff
        trend = f"{t.trend_pct_annual:+.1f}%/yr" if t.trend_pct_annual is not None else "trend unavailable"
        lines.append(f"Tariff: {t.current_rate:.4f} {t.currency}/kWh ({t.region_label}), recent trend {trend}")
    else:
        lines.append("Tariff: unavailable")

    sc = result.scores
    lines.append(f"Scores (0-100): physical {sc.physical}, shared infrastructure {sc.shared_infra}, financial {sc.financial if sc.financial is not None else 'n/a'}, composite {sc.composite} ({sc.band})")
    if result.projection:
        p = result.projection
        exposure = f"{p.annual_exposure:,.0f} {result.tariff.currency}/yr" if (p.annual_exposure is not None and result.tariff) else "n/a"
        default_note = " (assumed default consumption)" if p.annual_mwh_is_default else ""
        lines.append(f"Projection: infrastructure impact {p.infra_impact_pct_3yr}% over 3 yrs, annual increase {p.annual_increase_pct}%, year-end rate {p.year_end_rate:.4f}, exposure {exposure}{default_note}")

    lines.append("PROVENANCE")
    for key, prov in result.provenance.items():
        note = f" — {prov.note}" if prov.note else ""
        lines.append(f"  {key}: {prov.status} ({prov.source}){note}")
    return "\n".join(lines)


OUTPUT_INSTRUCTIONS = """Return JSON with:
- "synthesis": 3-5 sentences on the core issue, why it compounds, and the business implication.
- "recommendations": exactly 3 specific, actionable items ordered by value.
- "key_findings": 2-4 short factual bullets drawn from the data."""


def build_prompt(result: Any) -> str:
    region = region_for(result.site.country_code)
    return "\n\n".join([PERSONA.strip(), REGION_FRAGMENTS[region], render_data_block(result), OUTPUT_INSTRUCTIONS])
