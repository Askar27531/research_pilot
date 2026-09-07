"""Tests for the M2 cross-modal consistency scanner."""

from pathlib import Path

import pytest

from app.evidence.consistency import (
    CrossModalConsistencyChecker,
    _consistency_user_content,
    consensus_status,
)
from app.schemas import (
    BoundingBox,
    CrossModalConsistencyReport,
    DocumentFigure,
    EvidenceNode,
    FigureMention,
    ParsedDocument,
    ProseConsistencyCheck,
)

HASH64 = "a" * 64


def _figure_node(source_path: str = "figures/f.png") -> EvidenceNode:
    return EvidenceNode.model_validate({
        "evidence_id": "fig",
        "project_id": "project",
        "paper_id": "paper",
        "document_id": "document",
        "evidence_type": "figure",
        "claim": "claim text",
        "confidence": 1,
        "page_number": 5,
        "label": "Fig. 1",
        "excerpt": "Fig. 1 Results.",
        "source_path": source_path,
        "source_hash": HASH64,
        "created_at": "2026-09-03T00:00:00Z",
    })


def _parsed(crop: str, mention: FigureMention | None) -> ParsedDocument:
    figure = DocumentFigure(
        figure_id="figure-1", document_id="document", page_number=5,
        label="Fig. 1", caption="Fig. 1 Results.", figure_type="result",
        bbox=BoundingBox(x0=0, y0=0, x1=1, y1=1), source_path=crop,
        sha256=HASH64, mentions=[mention] if mention else [],
    )
    return ParsedDocument(
        document_id="document", project_id="project", page_count=10,
        pages=[], sections=[], figures=[figure], tables=[],
    )


class _FakeProvider:
    def __init__(self, report: CrossModalConsistencyReport) -> None:
        self.report = report
        self.calls: list[tuple[list[dict], list[bytes]]] = []

    async def structured_output_with_images(self, messages, images, response_model):
        self.calls.append((messages, images))
        return self.report


class _PlainProvider:
    pass


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
        self.workspace = self

    def resolve_safe_path(self, project_id, relative):
        return self.files[relative]


def _checker(provider, research, docs):
    return CrossModalConsistencyChecker(provider, docs, research)


def test_consensus_status_mapping() -> None:
    assert consensus_status(["consistent"]) == "confirmed"
    assert consensus_status(["consistent", "unverifiable"]) is None
    assert consensus_status(["unverifiable"]) is None
    assert consensus_status(["consistent", "inconsistent"]) == "doubted"
    assert consensus_status([]) is None


def test_user_content_numbers_mentions() -> None:
    mentions = [
        FigureMention(page_number=2, sentence="We improve accuracy as in Fig. 1."),
        FigureMention(page_number=3, sentence="Ablations are shown in Fig. 1."),
    ]
    content = _consistency_user_content("Fig. 1 Results.", mentions)

    assert "[1] (p2) We improve accuracy as in Fig. 1." in content
    assert "[2] (p3) Ablations are shown in Fig. 1." in content
    assert "Fig. 1 Results." in content


@pytest.mark.asyncio
async def test_conflict_is_written_as_doubted(tmp_path) -> None:
    crop = tmp_path / "f.png"
    crop.write_bytes(b"fake-png")
    provider = _FakeProvider(CrossModalConsistencyReport(checks=[
        ProseConsistencyCheck(
            mention_index=1, status="inconsistent",
            visible_evidence="The baseline curve is above Ours everywhere.",
            note="The prose says Ours wins, but the figure shows the opposite.",
        ),
    ]))
    research = _FakeResearch()
    mention = FigureMention(page_number=2, sentence="Ours beats the baseline in Fig. 1.")
    docs = _FakeDocuments({"figures/f.png": crop})
    checker = _checker(provider, research, docs)

    summary = await checker.check_evidence(
        "project", _parsed("figures/f.png", mention), _figure_node(), trace_id="t1"
    )

    assert summary["inconsistent"] == 1
    assert len(provider.calls) == 1
    user = provider.calls[0][0][-1]["content"]
    assert "Ours beats the baseline in Fig. 1." in user
    assert research.written == [(
        "fig", "doubted",
        "图文一致：存疑：[1] The prose says Ours wins, but the figure shows the opposite.",
    )]


@pytest.mark.asyncio
async def test_no_mentions_skips_scan(tmp_path) -> None:
    crop = tmp_path / "f.png"
    crop.write_bytes(b"fake-png")
    provider = _FakeProvider(CrossModalConsistencyReport(checks=[
        ProseConsistencyCheck(mention_index=1, status="consistent",
                              visible_evidence="Unused placeholder."),
    ]))
    research = _FakeResearch()
    docs = _FakeDocuments({"figures/f.png": crop})
    checker = _checker(provider, research, docs)

    summary = await checker.check_evidence(
        "project", _parsed("figures/f.png", None), _figure_node()
    )

    assert provider.calls == []
    assert research.written == []
    assert summary["checked"] == 0


@pytest.mark.asyncio
async def test_already_reviewed_evidence_is_skipped(tmp_path) -> None:
    crop = tmp_path / "f.png"
    crop.write_bytes(b"fake-png")
    provider = _FakeProvider(CrossModalConsistencyReport(checks=[
        ProseConsistencyCheck(mention_index=1, status="consistent",
                              visible_evidence="Matches."),
    ]))
    research = _FakeResearch(reviewed={"fig"})
    docs = _FakeDocuments({"figures/f.png": crop})
    checker = _checker(provider, research, docs)

    summary = await checker.check_evidence(
        "project", _parsed(
            "figures/f.png",
            FigureMention(page_number=2, sentence="See Fig. 1."),
        ), _figure_node()
    )

    assert provider.calls == []
    assert research.written == []
    assert summary["checked"] == 0


@pytest.mark.asyncio
async def test_provider_without_vision_is_skipped(tmp_path) -> None:
    crop = tmp_path / "f.png"
    crop.write_bytes(b"fake-png")
    checker = _checker(_PlainProvider(), _FakeResearch(),
                       _FakeDocuments({"figures/f.png": crop}))

    summary = await checker.check_evidence(
        "project", _parsed(
            "figures/f.png",
            FigureMention(page_number=2, sentence="See Fig. 1."),
        ), _figure_node()
    )

    assert summary == {"consistent": 0, "inconsistent": 0, "unverifiable": 0,
                       "checked": 0}
