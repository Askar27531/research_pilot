from pathlib import Path

from evals.evaluators import evaluate_dataset


def test_literature_gold_set_metrics() -> None:
    dataset = Path(__file__).parents[2] / "evals" / "datasets" / "literature_rgb_lwir_gold_v1.json"
    result = evaluate_dataset(dataset)
    assert result.dataset_id == "literature-rgb-lwir-gold-v1"
    assert result.true_positive == 6
    assert result.false_positive == 0
    assert result.false_negative == 0
    assert result.true_negative == 6
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0
