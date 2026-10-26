"""Persistence contract plus a JSON-file implementation.

Layout under ``root``::

    assessments/<assessment_id>.json   full AssessmentResult
    index.json                         {"latest": {site_key: summary}, "history": {site_key: [summary, ...]}}

History is newest first and capped at :data:`HISTORY_LIMIT` entries per site. File I/O runs in a
thread so the async pipeline is not blocked. Implement :class:`Store` for a database.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union

from gridscout import settings
from gridscout.models import AssessmentResult

HISTORY_LIMIT = 50


def summary(result: AssessmentResult) -> Dict[str, Any]:
    """Scalar columns for lists and dashboards, so callers need not parse the full result."""
    s, site, tariff, proj = result.scores, result.site, result.tariff, result.projection
    return {
        "assessment_id": result.assessment_id,
        "site_key": site.site_key,
        "site_name": site.name,
        "is_adhoc": site.is_adhoc,
        "country_code": site.country_code,
        "lat": site.lat,
        "lon": site.lon,
        "composite": s.composite,
        "physical": s.physical,
        "shared_infra": s.shared_infra,
        "financial": s.financial,
        "band": s.band,
        "coverage": s.coverage,
        "datacenter_count": len(result.threats),
        "total_capacity_mw": result.total_capacity_mw,
        "substation_name": result.substation.name if result.substation else None,
        "currency": tariff.currency if tariff else None,
        "current_rate": tariff.current_rate if tariff else None,
        "annual_increase_pct": proj.annual_increase_pct if proj else None,
        "annual_exposure": proj.annual_exposure if proj else None,
        "created_at": result.created_at.isoformat(),
        "schema_version": result.schema_version,
    }


class Store(Protocol):
    async def save_assessment(self, result: AssessmentResult) -> None: ...

    async def get_assessment(self, assessment_id: str) -> Optional[AssessmentResult]: ...

    async def latest_for_site(self, site_key: str) -> Optional[Dict[str, Any]]: ...

    async def list_latest(self) -> List[Dict[str, Any]]: ...

    async def history(self, site_key: str, limit: int = 12) -> List[Dict[str, Any]]: ...


class JsonFileStore:
    def __init__(self, root: Optional[Union[str, Path]] = None):
        self.root = Path(root or settings.STORE_DIR)
        self._lock = threading.Lock()

    # -- files ------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _assessment_path(self, assessment_id: str) -> Path:
        safe = "".join(c for c in assessment_id if c.isalnum() or c in "-_")
        return self.root / "assessments" / f"{safe}.json"

    def _read_index(self) -> Dict[str, Any]:
        if not self._index_path.exists():
            return {"latest": {}, "history": {}}
        return json.loads(self._index_path.read_text(encoding="utf-8"))

    def _write_index(self, index: Dict[str, Any]) -> None:
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=1, default=str), encoding="utf-8")
        tmp.replace(self._index_path)

    # -- writes -----------------------------------------------------------

    def _save_sync(self, result: AssessmentResult) -> None:
        with self._lock:
            (self.root / "assessments").mkdir(parents=True, exist_ok=True)
            self._assessment_path(result.assessment_id).write_text(result.model_dump_json(indent=1), encoding="utf-8")
            index = self._read_index()
            row = summary(result)
            index["latest"][result.site.site_key] = row
            hist = index["history"].setdefault(result.site.site_key, [])
            hist.insert(0, row)
            del hist[HISTORY_LIMIT:]
            self._write_index(index)

    async def save_assessment(self, result: AssessmentResult) -> None:
        await asyncio.to_thread(self._save_sync, result)

    # -- reads ------------------------------------------------------------

    def _get_sync(self, assessment_id: str) -> Optional[AssessmentResult]:
        path = self._assessment_path(assessment_id)
        if not path.exists():
            return None
        return AssessmentResult.model_validate_json(path.read_text(encoding="utf-8"))

    async def get_assessment(self, assessment_id: str) -> Optional[AssessmentResult]:
        return await asyncio.to_thread(self._get_sync, assessment_id)

    async def latest_for_site(self, site_key: str) -> Optional[Dict[str, Any]]:
        index = await asyncio.to_thread(self._read_index)
        return index["latest"].get(site_key)

    async def list_latest(self) -> List[Dict[str, Any]]:
        index = await asyncio.to_thread(self._read_index)
        return list(index["latest"].values())

    async def history(self, site_key: str, limit: int = 12) -> List[Dict[str, Any]]:
        index = await asyncio.to_thread(self._read_index)
        return list(index["history"].get(site_key, []))[:limit]


__all__ = ["Store", "JsonFileStore", "summary", "HISTORY_LIMIT"]
