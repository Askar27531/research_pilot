"""Real-figure cross-modal-consistency gold evaluation (v1).

Runs the production consistency prompt (skill + numbering + schema) against real
paper figure crops from the local workspace, with gold sentences authored from
each figure's caption semantics (see evals/datasets/real_figures_gold_v1.json).

Run:  python -m evals.run_real_consistency_eval
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.evidence.consistency import _consistency_user_content, consensus_status
from app.llm import OllamaProvider
from app.schemas import CrossModalConsistencyReport, FigureMention
from app.skills import SkillRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = REPO_ROOT / "evals" / "datasets" / "real_figures_gold_v1.json"
SNAPSHOT_PATH = REPO_ROOT / "evals" / "snapshots" / "real_figures_gold_v1_result.json"


def load_items() -> list[dict[str, Any]]:
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for figure in data["items"]:
        for check in figure["checks"]:
            items.append({
                "id": f"{figure['id']}-{check['gold']}",
                "image": figure["image"],
                "caption": figure["caption"],
                "mention": check["mention"],
                "gold": check["gold"],
            })
    return items


async def _run_case(provider, system: str, item: dict[str, Any]) -> dict:
    image_path = REPO_ROOT / item["image"]
    image = image_path.read_bytes()
    mention = FigureMention(page_number=1, sentence=item["mention"])
    user = _consistency_user_content(item["caption"], [mention])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    report = await provider.structured_output_with_images(
        messages, [image], CrossModalConsistencyReport
    )
    statuses = [check.status for check in report.checks]
    model_status = statuses[0] if statuses else None
    decision = consensus_status(statuses)
    expected_decision = {"consistent": "confirmed", "inconsistent": "doubted"}.get(
        item["gold"]
    )
    return {
        "id": item["id"], "gold": item["gold"], "mention": item["mention"],
        "model_status": model_status, "statuses": statuses, "decision": decision,
        "status_correct": model_status == item["gold"],
        "decision_correct": decision == expected_decision,
        "checks_raw": [
            {"index": check.mention_index, "status": check.status,
             "visible_evidence": check.visible_evidence, "note": check.note}
            for check in report.checks
        ],
    }


async def evaluate() -> dict[str, Any]:
    items = load_items()
    registry = SkillRegistry(REPO_ROOT / "skills")
    registry.discover()
    skill = registry.load_skill("cross-modal-consistency")
    results: list[dict] = []
    async with OllamaProvider(get_settings()) as provider:
        for index, item in enumerate(items, start=1):
            print(f"[real-gold] running {item['id']} (gold={item['gold']}) ...", flush=True)
            results.append(await _run_case(provider, skill.content, item))
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    by_class: dict[str, dict[str, int]] = {}
    for row in results:
        bucket = by_class.setdefault(row["gold"], {"correct": 0, "total": 0})
        bucket["total"] += 1
        if row["status_correct"]:
            bucket["correct"] += 1
    total = len(results)
    return {
        "model_checked": True,
        "total": total,
        "status_accuracy": round(sum(1 for r in results if r["status_correct"]) / total, 3),
        "decision_accuracy": round(
            sum(1 for r in results if r["decision_correct"]) / total, 3),
        "by_class": by_class,
        "results": results,
    }


async def main_async() -> None:
    metrics = await evaluate()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"real-figure consistency gold snapshot -> {SNAPSHOT_PATH.name}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
