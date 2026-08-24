import pytest
from pydantic import ValidationError

from app.schemas import ClaimValue, ComparisonCell


def test_supported_claim_and_comparison_cell_require_evidence() -> None:
    with pytest.raises(ValidationError):
        ClaimValue(value="A paper fact", kind="supported")
    with pytest.raises(ValidationError):
        ComparisonCell(value="A paper fact", kind="supported")


def test_inference_cannot_masquerade_as_cited_fact() -> None:
    with pytest.raises(ValidationError):
        ClaimValue(value="An inference", kind="inference", evidence_ids=["e1"])

    inference = ClaimValue(value="An inference", kind="inference")
    assert inference.evidence_ids == []
