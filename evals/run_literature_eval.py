from pathlib import Path

from evals.evaluators import evaluate_dataset


def main() -> None:
    dataset = Path(__file__).parent / "datasets" / "literature_rgb_lwir_gold_v1.json"
    result = evaluate_dataset(dataset)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

