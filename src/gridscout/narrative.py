"""Narrative contract: turn a scored assessment into a short written synthesis.

gridscout-core ships no model client. Implement :class:`Narrator` with whatever LLM you use,
build the prompt with :func:`gridscout.narrative_prompts.build_prompt`, and parse the JSON reply
with :func:`parse_narrative`. :class:`NullNarrator` is the default and marks the narrative
``unavailable`` so the assessment still completes with ``coverage="partial"``.
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from pydantic import BaseModel, Field

from gridscout.models import AssessmentResult, Narrative
from gridscout.narrative_prompts import build_prompt, region_for


class NarrativeSchema(BaseModel):
    """The JSON object a narrator asks its model to return (see ``OUTPUT_INSTRUCTIONS``)."""

    synthesis: str = Field(..., min_length=1)
    recommendations: List[str] = Field(..., min_length=1, max_length=5)
    key_findings: List[str] = Field(default_factory=list)


class Narrator(Protocol):
    name: str

    async def generate(self, result: AssessmentResult) -> Optional[Narrative]:
        """Return a Narrative, or None when generation failed. Must not raise."""
        ...


class NullNarrator:
    name = "none"

    async def generate(self, result: AssessmentResult) -> Optional[Narrative]:
        return None


def parse_narrative(text: str, model: str, country_code: str) -> Narrative:
    """Validate a model's JSON reply and trim it to the published shape (3 recommendations, 4 findings)."""
    parsed = NarrativeSchema.model_validate_json(text.strip())
    return Narrative(
        synthesis=parsed.synthesis.strip(),
        recommendations=[r.strip() for r in parsed.recommendations if r.strip()][:3],
        key_findings=[k.strip() for k in parsed.key_findings if k.strip()][:4],
        model=model,
        region_prompt=region_for(country_code),
    )


__all__ = ["Narrator", "NullNarrator", "NarrativeSchema", "parse_narrative", "build_prompt"]
