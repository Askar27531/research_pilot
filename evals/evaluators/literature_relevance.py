import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.literature.filtering import filter_by_required_concepts
from app.schemas import PaperMetadata


class LiteratureEvalResult(BaseModel):
    dataset_id: str
    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    predicted_ids: list[str]


def evaluate_dataset(path: Path) -> LiteratureEvalResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload["papers"]
    papers = [PaperMetadata.model_validate(record) for record in records]
    result = filter_by_required_concepts(
        papers,
        payload["required_concept_groups"],
        payload["excluded_topics"],
    )
    predicted = {paper.stable_id for paper in result.included}
    expected = {record["stable_id"] for record in records if record["relevant"]}
    all_ids = {record["stable_id"] for record in records}
    true_positive = len(predicted & expected)
    false_positive = len(predicted - expected)
    false_negative = len(expected - predicted)
    true_negative = len(all_ids - predicted - expected)
    precision = true_positive / len(predicted) if predicted else 0.0
    recall = true_positive / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return LiteratureEvalResult(
        dataset_id=payload["dataset_id"],
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        true_negative=true_negative,
        precision=round(precision, 6),
        recall=round(recall, 6),
        f1=round(f1, 6),
        predicted_ids=sorted(predicted),
    )

