from pathlib import Path

from evals.runner import run_dataset


def main() -> None:
    root = Path(__file__).parent
    run = run_dataset(root / "datasets" / "researchpilot_eval_v1.json")
    output = root / "snapshots" / "researchpilot_eval_v1_result.json"
    output.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    print(run.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
