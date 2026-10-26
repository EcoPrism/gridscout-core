"""Minimal command-line front end for one assessment."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, Optional, Sequence

from gridscout import __version__
from gridscout.models import AssessmentResult
from gridscout.providers import base
from gridscout.service import AssessmentError, assess, site_from_address
from gridscout.store import JsonFileStore


def render(result: AssessmentResult) -> str:
    s, lines = result.scores, []
    region = result.site.country_code + (", " + result.site.state_code if result.site.state_code else "")
    lines.append(f"{result.site.name}  [{region}]")
    financial = s.financial if s.financial is not None else "n/a"
    lines.append(f"composite {s.composite} ({s.band}), physical {s.physical}, shared infrastructure {s.shared_infra}, "
                 f"financial {financial}, coverage {s.coverage}")
    lines.append(f"data centres within {result.radius_km:g} km: {len(result.threats)} (~{result.total_capacity_mw:,.0f} MW estimated)")
    for t in result.threats[:8]:
        lines.append(f"  {t.distance_km:5.2f} km  {t.threat_level:8s} {t.facility_class:10s} {t.name} ({t.operator})")
    if len(result.threats) > 8:
        lines.append(f"  ... and {len(result.threats) - 8} more")
    if result.substation:
        sub = result.substation
        kv = f" {sub.voltage_kv} kV" if sub.voltage_kv else ""
        lines.append(f"nearest substation: {sub.name}{kv}, {sub.distance_km} km")
    if result.tariff:
        t = result.tariff
        trend = f"{t.trend_pct_annual:+.2f}%" if t.trend_pct_annual is not None else "n/a"
        lines.append(f"tariff: {t.current_rate:.4f} {t.currency}/kWh ({t.region_label}), recent trend {trend}")
    if result.projection and result.tariff:
        p = result.projection
        flag = " (assumed consumption)" if p.annual_mwh_is_default else ""
        lines.append(f"projection: +{p.annual_increase_pct}%/yr, exposure {p.annual_exposure:,.0f} {result.tariff.currency}/yr "
                     f"on {p.assumed_annual_mwh:,.0f} MWh{flag}")
    if result.narrative:
        lines.append("")
        lines.append(result.narrative.synthesis)
        for r in result.narrative.recommendations:
            lines.append(f"  - {r}")
    lines.append("")
    lines.append("provenance: " + ", ".join(f"{k}={v.status}" for k, v in result.provenance.items()))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gridscout", description="Score a street address for grid stress from nearby data centres.")
    p.add_argument("address", help="street address, e.g. '21715 Filigree Ct, Ashburn, VA 20147'")
    p.add_argument("--country", required=True, help="ISO-3166-1 alpha-2 country code, e.g. US")
    p.add_argument("--mwh", type=float, default=None, help="annual electricity use in MWh (exposure uses a default when omitted)")
    p.add_argument("--radius", type=float, default=None, help="search radius in km (default from GRIDSCOUT_RADIUS_KM, 8)")
    p.add_argument("--force", action="store_true", help="ignore a fresh stored assessment")
    p.add_argument("--no-store", action="store_true", help="do not read or write the JSON file store")
    p.add_argument("--json", action="store_true", help="print the full AssessmentResult as JSON")
    p.add_argument("--quiet", action="store_true", help="suppress stage progress on stderr")
    p.add_argument("--version", action="version", version=f"gridscout-core {__version__}")
    return p


async def _run(args: argparse.Namespace) -> int:
    async def emit(event: str, data: Dict[str, Any]) -> None:
        if args.quiet or event != "stage":
            return
        detail = data.get("detail")
        suffix = "" if detail is None else "  " + str(detail)
        print(f"  {data['stage']:12s} {data['status']}{suffix}", file=sys.stderr)

    site = site_from_address(args.address, args.country, annual_mwh=args.mwh)
    store = None if args.no_store else JsonFileStore()
    try:
        result = await assess(site, emit=emit, force=args.force, store=store, radius_km=args.radius)
    except AssessmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await base.aclose_http_client()
    print(result.model_dump_json(indent=1) if args.json else render(result))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))
