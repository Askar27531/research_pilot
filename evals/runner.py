import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from evals.schema import EvalDataset, EvalItemResult, EvalRun, EvalTask


def _f1(expected: set[str], predicted: set[str]) -> dict[str, float]:
    if not expected and not predicted:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    true_positive = len(expected & predicted)
    precision = true_positive / len(predicted) if predicted else 0
    recall = true_positive / len(expected) if expected else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {"precision": precision, "recall": recall, "f1": f1}


def score_task(task: EvalTask) -> EvalItemResult:
    if task.category == "search":
        metrics = _f1(
            set(task.reference["relevant_ids"]), set(task.prediction["selected_ids"])
        )
        score = metrics["f1"]
    elif task.category == "figure":
        count_score = 1 - min(
            1,
            abs(task.reference["count"] - task.prediction["count"])
            / max(1, task.reference["count"]),
        )
        type_metrics = _f1(
            set(task.reference["types"]), set(task.prediction["types"])
        )
        metrics = {"count_score": count_score, "type_f1": type_metrics["f1"]}
        score = (count_score + type_metrics["f1"]) / 2
    elif task.category == "evidence":
        total = len(task.reference["claims"])
        supported = sum(
            bool(item.get("evidence_ids")) and item.get("source_valid", False)
            for item in task.prediction["claims"]
        )
        support_rate = supported / total if total else 0
        metrics = {"support_rate": support_rate}
        score = support_rate
    else:
        required = set(task.reference["required_fields"])
        present = set(task.prediction["present_fields"])
        completeness = len(required & present) / len(required)
        reviews = task.prediction["review_scores"]
        reviewer_score = sum(reviews) / (len(reviews) * 5)
        metrics = {"completeness": completeness, "reviewer_score": reviewer_score}
        score = (completeness + reviewer_score) / 2
    return EvalItemResult(
        task_id=task.task_id,
        category=task.category,
        score=score,
        passed=score >= 0.8,
        metrics=metrics,
    )


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def run_dataset(path: Path) -> EvalRun:
    dataset = EvalDataset.model_validate_json(path.read_text(encoding="utf-8"))
    results = [score_task(task) for task in dataset.tasks]
    categories = sorted({item.category for item in results})
    aggregate = {
        "overall_score": sum(item.score for item in results) / len(results),
        "pass_rate": sum(item.passed for item in results) / len(results),
        **{
            f"{category}_score": sum(
                item.score for item in results if item.category == category
            )
            / sum(item.category == category for item in results)
            for category in categories
        },
    }
    settings = get_settings()
    return EvalRun(
        run_id=str(uuid4()),
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        started_at=datetime.now(UTC).isoformat(),
        git_commit=_git_commit(),
        model_name=settings.ollama_model,
        model_digest=os.getenv("OLLAMA_MODEL_DIGEST", "unrecorded"),
        parameters={"ollama_timeout_seconds": settings.ollama_timeout_seconds},
        environment={"python": platform.python_version(), "platform": platform.platform()},
        results=results,
        aggregate=aggregate,
        ablations=dataset.ablations,
    )
