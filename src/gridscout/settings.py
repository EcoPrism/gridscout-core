"""Runtime settings, read once from ``GRIDSCOUT_*`` environment variables.

Module-level constants on purpose: tests patch them with ``unittest.mock.patch.object`` and a host
application can assign them at start-up. Provider API keys use their vendor's conventional names.
"""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.getenv(f"GRIDSCOUT_{name}", default)


# -- assessment ---------------------------------------------------------------
RADIUS_KM = float(_env("RADIUS_KM", "8"))  # search radius for data centres (8 km ~ 5 miles)
MAX_THREATS = int(_env("MAX_THREATS", "15"))  # facilities kept in the payload, highest class first
FRESH_HOURS = int(_env("FRESH_HOURS", "168"))  # a full-coverage assessment younger than this is reused
DEFAULT_ANNUAL_MWH = float(_env("DEFAULT_ANNUAL_MWH", "120"))  # exposure assumption when consumption is unknown

# -- upstream services --------------------------------------------------------
OVERPASS_URL = _env("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
USER_AGENT = _env("USER_AGENT", "gridscout-core/0.1 (+https://github.com/EcoPrism/gridscout-core)")
NOMINATIM_USER_AGENT = _env("NOMINATIM_USER_AGENT", "gridscout-core")  # Nominatim policy: identify your app
PEERINGDB_API_KEY = os.getenv("PEERINGDB_API_KEY", "")  # optional; raises the PeeringDB rate limit
EIA_API_KEY = os.getenv("EIA_API_KEY", "")  # required for US tariffs; free at https://www.eia.gov/opendata/

# -- persistence --------------------------------------------------------------
STORE_DIR = _env("STORE_DIR", ".gridscout")  # JsonFileStore root, relative to the working directory
