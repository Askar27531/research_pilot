"""Declarative binding between structured outputs and the workflow skill that governs them.

Keeping the mapping here (instead of inline in an agent) gives tests a stable, lightweight
surface for the skill-integrity guard: every registered skill must be referenced here or by
a `load_skill(...)` call, and every reference must resolve to a registered skill.
"""

from collections.abc import Mapping
from typing import Any

from app.schemas import PaperScreeningBatch, SearchQueryPlan

SKILL_FOR_RESPONSE_MODEL: Mapping[type[Any], str] = {
    SearchQueryPlan: "systematic-search",
    PaperScreeningBatch: "paper-screening",
}
