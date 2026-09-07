"""Live cross-modal-consistency gold evaluation against the deployed vision model.

Renders deterministic synthetic charts (bars / rising curve) whose visible facts
are known, pairs each with a prose mention whose ground truth is
consistent / inconsistent / unverifiable, and runs the *production* prompt
(cross-modal-consistency skill + numbering + CrossModalConsistencyReport schema)
through Ollama. Reports per-item statuses, implied review decisions and accuracy
per class.

Run:  python -m evals.run_consistency_eval      (requires the vision model up)
This is NOT part of the CI suite: it needs a live local vision model.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pymupdf

from app.core.config import get_settings
from app.evidence.consistency import _consistency_user_content, consensus_status
from app.llm import OllamaProvider
from app.schemas import CrossModalConsistencyReport, FigureMention
from app.skills import SkillRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = REPO_ROOT / "evals" / "datasets" / "consistency_gold_v1.json"
SNAPSHOT_PATH = REPO_ROOT / "evals" / "snapshots" / "consistency_gold_v1_result.json"

PAGE_W = 500
PAGE_H = 320
AXIS_BASE = 265
BAR_COLOR = (0.45, 0.62, 0.85)
LINE_COLOR = (0.72, 0.25, 0.2)


def _text(page: pymupdf.Page, x: float, y: float, value: str, size: float = 13) -> None:
    page.insert_text((x, y), value, fontname="helv", fontsize=size)


def _render_chart(kind: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    # white background
    page.draw_rect(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), color=(1, 1, 1), fill=(1, 1, 1))
    page.draw_line((60, AXIS_BASE), (470, AXIS_BASE), color=(0, 0, 0), width=1.2)
    page.draw_line((60, 30), (60, AXIS_BASE), color=(0, 0, 0), width=1.2)
    if kind == "acc":
        _text(page, 205, 24, "Accuracy", size=15)
        bars = [
            (150, 0.80, "A", "0.80"),
            (320, 0.50, "B", "0.50"),
        ]
        for x_center, value, label, value_text in bars:
            height = value * 150
            x0 = x_center - 45
            rect = pymupdf.Rect(x0, AXIS_BASE - height, x0 + 90, AXIS_BASE)
            page.draw_rect(rect, color=(0, 0, 0), fill=BAR_COLOR, width=1)
            _text(page, x_center - 14, AXIS_BASE - height - 8, value_text, size=13)
            _text(page, x_center - 4, AXIS_BASE + 20, label, size=14)
        _text(page, 60, 40, "model A: 0.80, model B: 0.50", size=10)
    elif kind == "trend":
        _text(page, 200, 24, "Training metric", size=15)
        points = [(80 + index * 40, AXIS_BASE - 24 - index * 18) for index in range(10)]
        page.draw_polyline(points, color=LINE_COLOR, width=2.2)
        _text(page, 78, AXIS_BASE - 26, "start", size=9)
        _text(page, 400, AXIS_BASE - 200, "end", size=9)
        _text(page, 60, AXIS_BASE + 22, "epochs", size=11)
        _text(page, 24, 60, "metric", size=11)
    doc.set_metadata({})
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    data = pixmap.tobytes("png")
    doc.close()
    return data


def _expected_decision(gold: str) -> str | None:
    if gold == "consistent":
        return "confirmed"
    if gold == "inconsistent":
        return "doubted"
    return None


def load_items() -> list[dict[str, Any]]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))["items"]


async def _run_case(provider, system: str, index: int, item: dict[str, Any]) -> dict:
    png = _render_chart(item["chart"])
    caption = ("Fig. 3 Accuracy of two models." if item["chart"] == "acc"
               else "Fig. 3 Training metric over epochs.")
    mention = FigureMention(page_number=1, sentence=item["mention"])
    user = _consistency_user_content(caption, [mention])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    report = await provider.structured_output_with_images(
        messages, [png], CrossModalConsistencyReport
    )
    statuses = [check.status for check in report.checks]
    model_status = statuses[0] if statuses else None
    decision = consensus_status(statuses)
    return {
        "id": item["id"], "chart": item["chart"], "gold": item["gold"],
        "mention": item["mention"], "model_status": model_status,
        "statuses": statuses, "decision": decision,
        "status_correct": model_status == item["gold"],
        "decision_correct": decision == _expected_decision(item["gold"]),
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
            print(f"[consistency-gold] running {item['id']} "
                  f"(gold={item['gold']}) ...", flush=True)
            results.append(await _run_case(provider, skill.content, index, item))
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    shutil.rmtree(REPO_ROOT / "data" / "_consistency_gold", ignore_errors=True)
    by_class: dict[str, dict[str, int]] = {}
    correct = sum(1 for row in results if row["status_correct"])
    decision_correct = sum(1 for row in results if row["decision_correct"])
    for row in results:
        bucket = by_class.setdefault(row["gold"], {"correct": 0, "total": 0})
        bucket["total"] += 1
        if row["status_correct"]:
            bucket["correct"] += 1
    return {
        "model_checked": True,
        "total": len(results),
        "status_accuracy": round(correct / len(results), 3) if results else None,
        "decision_accuracy": round(decision_correct / len(results), 3) if results else None,
        "by_class": by_class,
        "results": results,
    }


async def main_async() -> None:
    metrics = await evaluate()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"consistency gold snapshot -> {SNAPSHOT_PATH.name}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
