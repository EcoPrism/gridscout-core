"""gridscout-core: score a street address for electricity-grid stress from nearby data-centre build-out.

    from gridscout import assess, site_from_address
    result = await assess(site_from_address("21715 Filigree Ct, Ashburn, VA 20147", "US"))
"""

from gridscout.models import AssessmentResult, Narrative, SiteRef
from gridscout.service import AssessmentError, Providers, assess, site_from_address

__version__ = "0.1.0"

__all__ = ["assess", "site_from_address", "AssessmentError", "Providers", "AssessmentResult", "SiteRef", "Narrative", "__version__"]
