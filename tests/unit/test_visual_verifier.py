"""Tests for the skill-driven automatic visual review pass (B2, M1)."""

from pathlib import Path

import pytest

from app.evidence.visual_verifier import (
    VisualEvidenceVerifier,
    _review_targets,
)
from app.schemas import (
    BlindVisualFacts,
    BoundingBox,
    DocumentFigure,
    EvidenceNode,
    FigureMention,
    ParsedDocument,
    TableCandidate,
    VerificationRegion,
    VisualVerificationVerdict,
)

HASH64 = "a" * 64


def _evidence(identifier: str, kind: str, source_path: str) -> EvidenceNode:
    common = {
        "evidence_id": identifier,
        "project_id": "project",
        "paper_id": "paper",
        "document_id": "document",
        "evidence_type": kind,
        "claim": "claim text for the evidence",
        "confidence": 1,
        "page_number": 2 if kind == "table" else 5,
        "source_path": source_path,
        "source_hash": HASH64,
        "created_at": "2026-09-03T00:00:00Z",
    }
    if kind == "text":
        common.update({"excerpt": "quote", "span_start": 0, "span_end": 5})
    else:
        common.update({
            "label": "Fig. 3" if kind == "figure" else "Table 2",
            "excerpt": "Fig. 3 Results." if kind == "figure" else "Table 2 Results.",
        })
    return EvidenceNode.model_validate(common)


def _parsed(crop_path: str, kind: str, mention: FigureMention | None) -> ParsedDocument:
    common = {
        "document_id": "document", "project_id": "project", "page_count": 10,
        "pages": [], "sections": [],
    }
    if kind == "figure":
        figure = DocumentFigure(
            figure_id="figure-1", document_id="document", page_number=5,
            label="Fig. 3", caption="Fig. 3 Results.", figure_type="result",
            bbox=BoundingBox(x0=0, y0=0, x1=1, y1=1), source_path=crop_path,
            sha256=HASH64, mentions=[mention] if mention else [],
        )
        return ParsedDocument(**common, figures=[figure], tables=[])
    table = TableCandidate(
        table_id="table-1", document_id="document", page_number=2, label="Table 2",
        caption="Table 2 Results.", cells=[], source_path=crop_path, sha256=HASH64,
        mentions=[mention] if mention else [],
    )
    return ParsedDocument(**common, figures=[], tables=[table])


class _FakeProvider:
    """Vision provider double: phase A returns facts, phase B returns the verdict."""

    def __init__(self, verdict: VisualVerificationVerdict,
                 facts: BlindVisualFacts | None = None) -> None:
        self.verdict = verdict
        self.facts = facts or BlindVisualFacts(
            visible_facts=["The curve labelled Ours is above the baseline everywhere."],
            unknowns=["Training cost is not shown."],
        )
        self.calls: list[tuple[list[dict], list[bytes]]] = []

    async def structured_output_with_images(self, messages, images, response_model):
        self.calls.append((messages, images))
        if response_model is BlindVisualFacts:
            return self.facts
        return self.verdict


class _PlainProvider:
    """A provider without the vision method (e.g. some test doubles)."""


class _FakeResearch:
    def __init__(self, reviewed: set[str] | None = None) -> None:
        self.reviewed = reviewed or set()
        self.written: list[tuple[str, str, str | None]] = []

    async def reviewed_evidence_ids(self, project_id, evidence_ids):
        return {item for item in evidence_ids if item in self.reviewed}

    async def review_evidence(self, project_id, evidence_id, status, note, **kwargs):
        self.written.append((evidence_id, status, note))


class _FakeDocuments:
    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files
        self.workspace = self  # verifier calls documents.workspace.resolve_safe_path

    def resolve_safe_path(self, project_id: str, relative: str) -> Path:
        return self.files[relative]


def _figure_verifier(files: dict[str, Path], provider, research):
    docs = _FakeDocuments(files)
    mention = FigureMention(page_number=2, sentence="We improve accuracy as in Fig. 3.")
    parsed = _parsed("figures/f.png", "figure", mention)
    by_id = {
        "fig": _evidence("fig", "figure", "figures/f.png"),
        "txt": _evidence("txt", "text", "pages/5.png"),
    }
    claims = [("方法在数据集上准确率显著提升", ["fig", "txt"])]
    verifier = VisualEvidenceVerifier(provider, docs, research)
    return verifier, parsed, claims, by_id


def test_review_targets_keeps_only_visual_evidence() -> None:
    by_id = {
        "fig": _evidence("fig", "figure", "figures/f.png"),
        "tab": _evidence("tab", "table", "pages/2.png"),
        "txt": _evidence("txt", "text", "pages/5.png"),
    }

    targets = _review_targets(
        [("claim-one", ["fig", "txt"]), ("claim-two", ["tab", "missing"])], by_id
    )

    assert targets == {"fig": "claim-one", "tab": "claim-two"}


def test_review_targets_empty_without_visual_citations() -> None:
    by_id = {"txt": _evidence("txt", "text", "pages/5.png")}
    assert _review_targets([("claim", ["txt"])], by_id) == {}


@pytest.mark.asyncio
async def test_verifier_runs_blind_then_verdict_and_writes_status(tmp_path) -> None:
    crop = tmp_path / "fig.png"
    crop.write_bytes(b"fake-png")
    provider = _FakeProvider(VisualVerificationVerdict(
        status="confirmed", reason="The plot clearly shows the stated improvement.",
        confidence=0.92,
        regions=[VerificationRegion(
            bbox=BoundingBox(x0=0.1, y0=0.2, x1=0.5, y1=0.4),
            note="Ours curve above baseline",
        )],
    ))
    research = _FakeResearch()
    verifier, parsed, claims, by_id = _figure_verifier(
        {"figures/f.png": crop}, provider, research
    )

    summary = await verifier.verify_cited_visual_evidence(
        "project", parsed, claims, by_id
    )

    assert summary == {"confirmed": 1, "doubted": 0, "excluded": 0, "failed": 0}
    assert len(provider.calls) == 2  # phase A (blind) + phase B (verdict)
    # Phase A must not see the claim (anti self-confirmation bias).
    blind_user = provider.calls[0][0][-1]["content"]
    assert "方法在数据集上准确率显著提升" not in blind_user
    assert "We improve accuracy as in Fig. 3." in blind_user
    # Phase B sees claim + blind facts together.
    verdict_user = provider.calls[1][0][-1]["content"]
    assert "方法在数据集上准确率显著提升" in verdict_user
    assert "The curve labelled Ours is above the baseline everywhere." in verdict_user
    assert "Unknowns recorded in the blind pass" in verdict_user
    assert provider.calls[0][1] == [b"fake-png"]
    assert provider.calls[1][1] == [b"fake-png"]
    assert research.written == [(
        "fig", "confirmed", "自动复核：The plot clearly shows the stated improvement.",
    )]


@pytest.mark.asyncio
async def test_verifier_skips_already_reviewed_evidence(tmp_path) -> None:
    crop = tmp_path / "fig.png"
    crop.write_bytes(b"fake-png")
    provider = _FakeProvider(VisualVerificationVerdict(
        status="excluded", reason="No such metric appears.", confidence=0.8,
    ))
    research = _FakeResearch(reviewed={"fig"})
    verifier, parsed, claims, by_id = _figure_verifier(
        {"figures/f.png": crop}, provider, research
    )

    summary = await verifier.verify_cited_visual_evidence(
        "project", parsed, claims, by_id
    )

    assert summary == {"confirmed": 0, "doubted": 0, "excluded": 0, "failed": 0}
    assert provider.calls == []
    assert research.written == []


@pytest.mark.asyncio
async def test_provider_without_vision_method_is_skipped(tmp_path) -> None:
    crop = tmp_path / "fig.png"
    crop.write_bytes(b"fake-png")
    provider = _PlainProvider()
    research = _FakeResearch()
    verifier, parsed, claims, by_id = _figure_verifier(
        {"figures/f.png": crop}, provider, research
    )

    summary = await verifier.verify_cited_visual_evidence(
        "project", parsed, claims, by_id
    )

    assert summary == {"confirmed": 0, "doubted": 0, "excluded": 0, "failed": 0}
    assert research.written == []


@pytest.mark.asyncio
async def test_verifier_uses_table_crop_and_mentions(tmp_path) -> None:
    page_shot = tmp_path / "page.png"
    crop = tmp_path / "table.png"
    page_shot.write_bytes(b"page-shot")
    crop.write_bytes(b"table-crop")
    provider = _FakeProvider(VisualVerificationVerdict(
        status="doubted", reason="The table lacks the claimed metric.", confidence=0.6,
    ))
    research = _FakeResearch()
    docs = _FakeDocuments({
        "pages/2.png": page_shot,       # evidence node points at the page screenshot
        "tables/t.png": crop,           # parsed structure keeps the real crop
    })
    mention = FigureMention(page_number=3, sentence="Table 2 lists per-dataset scores.")
    parsed = _parsed("tables/t.png", "table", mention)
    table_node = _evidence("tab", "table", "pages/2.png")
    by_id = {"tab": table_node}
    claims = [("表 2 给出了逐数据集分数", ["tab"])]
    verifier = VisualEvidenceVerifier(provider, docs, research)

    summary = await verifier.verify_cited_visual_evidence(
        "project", parsed, claims, by_id
    )

    assert summary["doubted"] == 1
    assert provider.calls[0][1] == [b"table-crop"]  # crop preferred over page shot
    assert provider.calls[1][1] == [b"table-crop"]
    verdict_user = provider.calls[1][0][-1]["content"]
    assert "Table 2 lists per-dataset scores." in verdict_user
    assert research.written == [
        ("tab", "doubted", "自动复核：The table lacks the claimed metric."),
    ]
