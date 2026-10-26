"""Provider contract, shared HTTP client and retry policy (LLD §5.3, §9).

Every provider:
- uses the one shared ``httpx.AsyncClient`` from :func:`get_http_client`,
- retries transport errors and HTTP 429/5xx three times with exponential backoff,
- returns ``ProviderResult(data=None, provenance.status="unavailable")`` on final failure,
- never fabricates data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Generic, Optional, Protocol, TypeVar

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from gridscout.models import Provenance

logger = logging.getLogger(__name__)

T = TypeVar("T")

from gridscout import settings

USER_AGENT = settings.USER_AGENT
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
RETRY_ATTEMPTS = 3
# Module-level so tests can swap in ``tenacity.wait_none()``.
RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=8)


@dataclass
class ProviderResult(Generic[T]):
    data: Optional[T]
    provenance: Provenance

    @property
    def ok(self) -> bool:
        return self.data is not None and self.provenance.status in ("real", "cached")


class Provider(Protocol):
    name: str

    async def fetch(self, **kw: Any) -> ProviderResult: ...


class RetryableStatus(Exception):
    """Raised for HTTP 429 and 5xx so tenacity retries them."""

    def __init__(self, status_code: int, url: str):
        super().__init__(f"HTTP {status_code} from {url}")
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Shared client
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    return _client


async def aclose_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=RETRY_WAIT,
        retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
        reraise=True,
    )


async def request_json(method: str, url: str, **kwargs: Any) -> Any:
    """One HTTP call with the shared retry policy. Raises on final failure."""
    client = get_http_client()
    async for attempt in retrying():
        with attempt:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableStatus(resp.status_code, url)
            resp.raise_for_status()
            return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc)


def real(source: str, data: T, cached: bool = False, note: Optional[str] = None) -> ProviderResult[T]:
    return ProviderResult(data=data, provenance=Provenance(source=source, status="cached" if cached else "real", fetched_at=now(), note=note))


def unavailable(source: str, note: str) -> ProviderResult[Any]:
    logger.warning("gridscout provider %s unavailable: %s", source, note)
    return ProviderResult(data=None, provenance=Provenance(source=source, status="unavailable", fetched_at=now(), note=note))


def failure_note(exc: BaseException) -> str:
    if isinstance(exc, RetryableStatus):
        return f"HTTP {exc.status_code} after {RETRY_ATTEMPTS} attempts"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return f"{type(exc).__name__} after {RETRY_ATTEMPTS} attempts"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def model_list(items: list, model: type) -> list:
    return [model.model_validate(i) if isinstance(i, dict) else i for i in items]


def dump_list(items: list) -> list:
    return [i.model_dump(mode="json") for i in items]


__all__ = [
    "Provider", "ProviderResult", "RetryableStatus", "get_http_client", "aclose_http_client", "retrying",
    "request_json", "real", "unavailable", "failure_note", "model_list", "dump_list", "now",
]

