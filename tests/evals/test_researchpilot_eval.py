from pathlib import Path

from evals.runner import run_dataset


def test_researchpilot_eval_has_twenty_tasks_and_reproducible_metrics() -> None:
    path = Path("evals/datasets/researchpilot_eval_v1.json")
    first = run_dataset(path)
    second = run_dataset(path)

    assert len(first.results) == 20
    assert {item.category for item in first.results} == {
        "search",
        "figure",
        "evidence",
        "experiment",
    }
    assert first.aggregate == second.aggregate
    assert first.aggregate["pass_rate"] == 1
    assert first.dataset_version == "1.0.0"
    assert "skills" in first.ablations and "context" in first.ablations
