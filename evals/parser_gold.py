"""P2-c gold evaluation for the stage-1 deterministic parser.

Renders a *synthetic but real* PDF (built with PyMuPDF, no network/models) that
must satisfy the expectations in evals/datasets/parser_gold_v1.json, runs the
production PDFParser over it and scores the outcome:

- figure region/caption recall for the gold label,
- prose-mention index coverage on the gold mention pages,
- semantic section typing for the gold titles,
- noise exclusion (running header, page numbers, references page),
- parse-result caching stability (second parse returns identical structure).

Run:  python -m evals.run_parser_eval
CI:    tests/unit/documents/test_parser_gold_eval.py enforces the same metrics.

Layout notes: headings are separated from following paragraphs by a generous
vertical gap so PyMuPDF keeps them in distinct text blocks (block-level roles
stay trustworthy).
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

import pymupdf

from app.documents.parser import PDFParser, is_reference_page
from app.documents.workspace import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = REPO_ROOT / "evals" / "datasets" / "parser_gold_v1.json"

LETTER_W = 612.0
LETTER_H = 792.0
HEADER_SIZE = 8.0
BODY_SIZE = 11.0
HEADING_SIZE = 16.0
BODY_STEP = 17.0
HEADING_GAP = 44.0


def _png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", checksum))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def load_gold() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _draw_body(page: pymupdf.Page, y: float, sentences: list[str]) -> float:
    for sentence in sentences:
        page.insert_text((72, y), sentence, fontname="helv", fontsize=BODY_SIZE)
        y += BODY_STEP
    return y


def build_gold_pdf(gold: dict[str, Any], target: Path) -> None:
    """Render the synthetic paper described by the gold dataset."""
    figure = gold["figure"]
    header = gold["noise"]["header"]
    pages_cfg = gold["pages"]
    page_count = pages_cfg["references_page"]

    doc = pymupdf.open()
    for page_number in range(1, page_count + 1):
        page = doc.new_page(width=LETTER_W, height=LETTER_H)
        y = 90.0
        page.insert_text((72, 14), header, fontname="helv", fontsize=HEADER_SIZE)
        if page_number == pages_cfg["figure_page"]:
            y = _draw_body(page, y, [
                "This paper studies a synthetic method for layout reading.",
                "We first describe the framework, then report experiments.",
            ])
            image_y = y + 16
            page.insert_image(
                pymupdf.Rect(110, image_y, 370, image_y + 150),
                stream=_png_bytes(320, 200, (220, 225, 240)),
            )
            caption_y = image_y + 178
            page.insert_text((110, caption_y), figure["caption"],
                             fontname="helv", fontsize=BODY_SIZE)
        elif page_number == 2:
            page.insert_text((72, y), "3 Method", fontname="helv", fontsize=HEADING_SIZE)
            y = _draw_body(page, y + HEADING_GAP, [
                f"Our pipeline is shown in {figure['label']}; it has two stages.",
                "The first stage extracts layout blocks in reading order.",
                f"We ablate the design choices reported in {figure['label']}.",
            ])
        elif page_number == 3:
            page.insert_text((72, y), "4 Experiments", fontname="helv", fontsize=HEADING_SIZE)
            y = _draw_body(page, y + HEADING_GAP, [
                f"We report every setting in {figure['label']}.",
                "We compare against two baselines on synthetic layouts.",
            ])
        elif page_number == 4:
            page.insert_text((72, y), "References", fontname="helv", fontsize=HEADING_SIZE)
            y = _draw_body(page, y + HEADING_GAP, [
                "A. Author. A synthetic method for layout reading. 2026.",
                "B. Writer. Reading order in two-column PDFs. 2025.",
            ])
        page.insert_text((300, 778), str(page_number), fontname="helv", fontsize=HEADER_SIZE)
    doc.save(target)
    doc.close()


def _gold_pdf_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "gold.pdf"
        build_gold_pdf(load_gold(), pdf_path)
        return pdf_path.read_bytes()


def evaluate_parser(root: Path | None = None) -> dict[str, Any]:
    """Run the full gold evaluation and return per-metric results."""
    gold = load_gold()
    root = root or Path(tempfile.mkdtemp(prefix="parser-gold-"))
    workspace = WorkspaceManager(root)
    parser = PDFParser(workspace, render_dpi=96)
    entry = workspace.import_pdf_bytes("parser-gold-proj", "gold.pdf", _gold_pdf_bytes())
    parsed = parser.parse("parser-gold-proj", entry.document_id)

    figure = gold["figure"]
    header = gold["noise"]["header"]
    refs_page = gold["pages"]["references_page"]
    page_count = refs_page
    matching = [
        f for f in parsed.figures
        if (f.label or "").casefold() == figure["label"].casefold()
    ]
    mention_pages: set[int] = set()
    for f in matching:
        mention_pages.update(item.page_number for item in f.mentions)

    metrics: dict[str, Any] = {
        "figure_label_found": bool(matching),
        "figure_page_correct": bool(matching) and any(
            f.page_number == figure["page"] for f in matching
        ),
        "mentions_complete": set(gold["pages"]["mention_pages"]) <= mention_pages,
        "header_excluded": all(header not in page.text for page in parsed.pages),
        "page_numbers_removed": sum(
            1 for page in parsed.pages
            for block in page.blocks if block.role == "page_number"
        ) >= page_count,
        "references_page_excluded": is_reference_page(parsed, refs_page),
        "cache_stable": parser.parse(
            "parser-gold-proj", entry.document_id
        ).model_dump_json() == parsed.model_dump_json(),
        "sections_all_typed": all(
            any(
                section.type == expectation["type"]
                and expectation["title"].casefold() in section.title.casefold()
                for section in parsed.sections
            )
            for expectation in gold["sections"]
        ),
        "page_count": len(parsed.pages),
        "sections_detected": [
            (section.title, section.type) for section in parsed.sections
        ],
    }
    required = (
        "figure_label_found", "figure_page_correct", "mentions_complete",
        "sections_all_typed", "header_excluded", "page_numbers_removed",
        "references_page_excluded", "cache_stable",
    )
    metrics["all_pass"] = all(metrics[name] for name in required)
    return metrics


def main() -> None:
    metrics = evaluate_parser()
    snapshot = REPO_ROOT / "evals" / "snapshots" / "parser_gold_v1_result.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if not metrics["all_pass"]:
        raise SystemExit("parser gold evaluation failed")
    print(f"parser gold evaluation passed -> {snapshot.name}")


if __name__ == "__main__":
    main()
