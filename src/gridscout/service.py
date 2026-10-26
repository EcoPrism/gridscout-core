"""Assessment pipeline: coordinates -> providers -> scoring -> narrative -> persist.

Events are pushed through an async ``emit(event, data)`` callable so a host can stream them
(server-sent events, a progress bar, logs) while the pipeline runs. Every provider failure
degrades to ``unavailable`` provenance; only missing coordinates abort the assessment.

Event names: ``run``, ``stage`` (``{"stage", "status": start|done|unavailable|skipped, "detail"}``),
``partial`` (``{"section": threats|substation|tariff, "data"}``) and ``complete`` (the result).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gridscout import financial, scoring, settings
from gridscout.models import (
    AssessmentResult, Narrative, Provenance, RateProjection, SectionScores, SiteRef, Substation, TariffSnapshot, Threat,
)
from gridscout.narrative import NullNarrator
from gridscout.providers import base
from gridscout.providers.datacenters import PeeringDBProvider
from gridscout.providers.geocode import NominatimGeocoder
from gridscout.providers.substations import OverpassProvider
from gridscout.providers.tariffs import provider_for
from gridscout.store import Store

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, Dict[str, Any]], Awaitable[None]]


class AssessmentError(Exception):
    """A failure that prevents any assessment (e.g. no coordinates). Safe to show to the caller."""


@dataclass
class Providers:
    geocoder: Any = field(default_factory=NominatimGeocoder)
    datacenters: Any = field(default_factory=PeeringDBProvider)
    substations: Any = field(default_factory=OverpassProvider)
    tariff_for: Callable[[str], Any] = provider_for
    narrator: Any = field(default_factory=NullNarrator)


def site_from_address(address: str, country_code: str, annual_mwh: Optional[float] = None, name: Optional[str] = None) -> SiteRef:
    """Build an ad-hoc SiteRef. The key is stable for the same address and country."""
    digest = hashlib.sha1(f"{address.strip().lower()}|{country_code.upper()}".encode("utf-8")).hexdigest()[:16]
    return SiteRef(
        site_key=f"adhoc-{digest}", name=name or address.strip(), address=address.strip(),
        country_code=country_code.upper(), annual_mwh=annual_mwh, is_adhoc=True,
    )


async def _noop_emit(event: str, data: Dict[str, Any]) -> None:
    return None


def _fresh(latest: Optional[Dict[str, Any]], hours: int) -> bool:
    if not latest or latest.get("coverage") != "full" or not latest.get("created_at"):
        return False
    try:
        created = datetime.fromisoformat(str(latest["created_at"]))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(hours=hours)


async def assess(
    site: SiteRef,
    emit: Optional[EmitFn] = None,
    force: bool = False,
    providers: Optional[Providers] = None,
    store: Optional[Store] = None,
    radius_km: Optional[float] = None,
) -> AssessmentResult:
    """Run the full pipeline for one site. ``store=None`` skips the freshness check and persistence."""
    emit = emit or _noop_emit
    providers = providers or Providers()
    radius = radius_km or settings.RADIUS_KM
    started = time.monotonic()
    assessment_id = str(uuid.uuid4())
    provenance: Dict[str, Provenance] = {}

    await emit("run", {"assessment_id": assessment_id, "site_key": site.site_key, "name": site.name})

    # -- freshness short-circuit ------------------------------------------
    if store is not None and not force:
        latest = await store.latest_for_site(site.site_key)
        if _fresh(latest, settings.FRESH_HOURS):
            existing = await store.get_assessment(latest["assessment_id"])
            if existing is not None:
                await emit("stage", {"stage": "cached", "status": "done", "detail": existing.assessment_id})
                await emit("complete", existing.model_dump(mode="json"))
                return existing

    # -- coordinates ------------------------------------------------------
    await emit("stage", {"stage": "coordinates", "status": "start"})
    if site.lat is not None and site.lon is not None:
        provenance["coordinates"] = Provenance(source="caller", status="real", fetched_at=base.now())
    else:
        geo = await providers.geocoder.fetch(site.address, site.country_code or None)
        provenance["coordinates"] = geo.provenance
        if not geo.ok:
            await emit("stage", {"stage": "coordinates", "status": "unavailable", "detail": geo.provenance.note})
            raise AssessmentError(f"Could not geocode the address: {geo.provenance.note}")
        site = site.model_copy(update={
            "lat": geo.data.lat, "lon": geo.data.lon,
            "country_code": site.country_code or geo.data.country_code,
            "state_code": site.state_code or geo.data.state_code,
        })
    await emit("stage", {"stage": "coordinates", "status": "done", "detail": {"lat": site.lat, "lon": site.lon}})

    # -- providers in parallel --------------------------------------------
    for stage in ("datacenters", "substations", "tariff"):
        await emit("stage", {"stage": stage, "status": "start"})
    dc_res, sub_res, tariff_res = await asyncio.gather(
        providers.datacenters.fetch(site.lat, site.lon, radius),
        providers.substations.fetch(site.lat, site.lon, radius),
        providers.tariff_for(site.country_code).fetch(site),
    )
    provenance["datacenters"] = dc_res.provenance
    provenance["substations"] = sub_res.provenance
    provenance["tariff"] = tariff_res.provenance

    threats: List[Threat] = dc_res.data or []
    await emit("stage", {"stage": "datacenters", "status": "done" if dc_res.ok else "unavailable", "detail": len(threats) if dc_res.ok else dc_res.provenance.note})
    await emit("partial", {"section": "threats", "data": [t.model_dump(mode="json") for t in threats]})

    substations: List[Substation] = sub_res.data or []
    site_substation: Optional[Substation] = substations[0] if substations else None
    await emit("stage", {"stage": "substations", "status": "done" if sub_res.ok else "unavailable", "detail": len(substations) if sub_res.ok else sub_res.provenance.note})
    await emit("partial", {"section": "substation", "data": site_substation.model_dump(mode="json") if site_substation else None})

    tariff: Optional[TariffSnapshot] = tariff_res.data if tariff_res.ok else None
    await emit("stage", {"stage": "tariff", "status": "done" if tariff else "unavailable", "detail": tariff.region_label if tariff else tariff_res.provenance.note})
    await emit("partial", {"section": "tariff", "data": tariff.model_dump(mode="json") if tariff else None})

    # -- scoring ----------------------------------------------------------
    await emit("stage", {"stage": "scoring", "status": "start"})
    shared, _, _ = scoring.shared_infrastructure(threats, site_substation, substations)
    physical = scoring.physical_score(threats, radius, shared)
    total_mw = scoring.total_capacity_mw(threats)
    infra_impact = scoring.infra_impact_pct_3yr(total_mw, shared, len(threats))
    projection: Optional[RateProjection] = None
    financial_score: Optional[float] = None
    if tariff is not None:
        annual_incr = scoring.annual_increase_pct(tariff.trend_pct_annual, infra_impact)
        financial_score = scoring.financial_score(annual_incr)
        projection = financial.rate_projection(tariff, annual_incr, infra_impact, site.annual_mwh, default_annual_mwh=settings.DEFAULT_ANNUAL_MWH)
    comp = scoring.composite(physical, financial_score)
    scores = SectionScores(physical=physical, shared_infra=shared, financial=financial_score, composite=comp, band=scoring.band(comp),
                           coverage=scoring.coverage(tariff_res.provenance.status, False))
    await emit("stage", {"stage": "scoring", "status": "done", "detail": {"composite": comp, "band": scores.band}})

    draft = AssessmentResult(
        assessment_id=assessment_id, site=site, radius_km=radius, threats=threats, total_capacity_mw=total_mw,
        substation=site_substation, tariff=tariff, scores=scores, projection=projection, narrative=None,
        provenance=dict(provenance), created_at=datetime.now(timezone.utc), duration_ms=0,
    )

    # -- narrative --------------------------------------------------------
    narrator_name = getattr(providers.narrator, "name", "narrator")
    await emit("stage", {"stage": "narrative", "status": "start"})
    narrative: Optional[Narrative] = await providers.narrator.generate(draft)
    if narrative is not None:
        provenance["narrative"] = Provenance(source=narrator_name, status="real", fetched_at=base.now(), note=narrative.model)
        await emit("stage", {"stage": "narrative", "status": "done"})
    else:
        provenance["narrative"] = Provenance(source=narrator_name, status="unavailable", fetched_at=base.now(), note="narrative generation failed or no narrator configured")
        await emit("stage", {"stage": "narrative", "status": "unavailable"})

    scores = scores.model_copy(update={"coverage": scoring.coverage(tariff_res.provenance.status, narrative is not None)})
    result = draft.model_copy(update={
        "scores": scores, "narrative": narrative, "provenance": dict(provenance),
        "duration_ms": int((time.monotonic() - started) * 1000),
    })

    # -- persist ----------------------------------------------------------
    if store is None:
        await emit("stage", {"stage": "persist", "status": "skipped"})
    else:
        await emit("stage", {"stage": "persist", "status": "start"})
        try:
            await store.save_assessment(result)
            await emit("stage", {"stage": "persist", "status": "done"})
        except Exception as exc:  # noqa: BLE001 - a storage blip must not lose the computed result
            logger.exception("gridscout persist failed for %s", site.site_key)
            await emit("stage", {"stage": "persist", "status": "unavailable", "detail": f"{type(exc).__name__}"})

    await emit("complete", result.model_dump(mode="json"))
    return result


__all__ = ["assess", "AssessmentError", "Providers", "site_from_address", "EmitFn"]
