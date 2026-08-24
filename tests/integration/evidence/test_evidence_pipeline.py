from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from app.db import (
    Database,
    DocumentRepository,
    EvidenceRepository,
    PaperRepository,
    ProjectRepository,
    SummaryRepository,
)
from app.db.errors import EvidenceReferencedError, RecordNotFoundError
from app.documents import (
    DocumentService,
    DocumentValidationError,
    PDFParser,
    WorkspaceManager,
)
from app.evidence import (
    ContextCompactor,
    CrossPaperSynthesizer,
    EvidenceBuilder,
    EvidenceVerifier,
)
from app.schemas import (
    ClaimValue,
    PaperMetadata,
    PaperSummary,
    RankedPaper,
    ResearchRequest,
    TextEvidenceCreate,
)

COLUMNS = ["method", "datasets", "metrics", "contributions", "limitations"]


def create_pdf(path: Path, index: int) -> dict[str, str]:
    claims = {
        "method": f"Paper {index} uses method M{index}.",
        "datasets": f"Paper {index} evaluates dataset D{index}.",
        "metrics": f"Paper {index} reports metric score {80 + index}.",
        "contributions": f"Paper {index} contributes component C{index}.",
        "limitations": f"Paper {index} is limited by condition L{index}.",
    }
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Introduction")
    for line, value in enumerate(claims.values(), start=1):
        page.insert_text((72, 72 + line * 24), value)
    document.save(path)
    document.close()
    return claims


@pytest.mark.asyncio
async def test_five_paper_evidence_comparison_and_source_round_trip(tmp_path) -> None:
    database = Database(tmp_path / "evidence.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Evidence Gold Set", ResearchRequest(research_question="Compare five methods")
    )
    workspace = WorkspaceManager(tmp_path / "workspaces")
    service = DocumentService(workspace, PDFParser(workspace, render_dpi=72))
    document_repository = DocumentRepository(database)
    evidence_repository = EvidenceRepository(database)
    summary_repository = SummaryRepository(database)
    builder = EvidenceBuilder(service, document_repository, evidence_repository)
    first_evidence_id = None
    summaries = []

    for index in range(1, 6):
        ranked = RankedPaper(
            paper=PaperMetadata(
                stable_id=f"paper-{index}",
                source_id=f"W{index}",
                title=f"Paper {index}",
            ),
            lexical_score=0.8,
            llm_score=0.9,
            final_score=0.85,
            selection_reason="Gold set",
        )
        paper = await PaperRepository(database).upsert_ranked(project.id, ranked)
        source = tmp_path / f"paper-{index}.pdf"
        claims = create_pdf(source, index)
        entry = workspace.import_pdf(project.id, source)
        service.parse_document(project.id, entry.document_id)
        await document_repository.register(project.id, paper.id, entry)
        claim_values = {}
        for column, quote in claims.items():
            node = await builder.build_text(
                project.id,
                TextEvidenceCreate(
                    paper_id=paper.id,
                    document_id=entry.document_id,
                    page_number=1,
                    claim=quote,
                    quote=quote,
                    confidence=0.95,
                ),
            )
            if first_evidence_id is None:
                first_evidence_id = node.evidence_id
            claim_values[column] = [ClaimValue(value=quote, evidence_ids=[node.evidence_id])]
            assert (
                service.get_page(project.id, entry.document_id, 1).text[
                    node.span_start : node.span_end
                ]
                == node.excerpt
            )
        summary = PaperSummary(
            summary_id=str(uuid4()),
            project_id=project.id,
            paper_id=paper.id,
            created_at=datetime.now(UTC).isoformat(),
            **claim_values,
        )
        summaries.append(await summary_repository.upsert(summary))

    comparison = CrossPaperSynthesizer().build(project.id, summaries)
    assert len(comparison.rows) == 5
    assert comparison.columns == COLUMNS
    assert all(
        cell.kind == "supported" and cell.evidence_ids
        for row in comparison.rows
        for cell in row.cells.values()
    )
    assert len(await evidence_repository.list_for_paper(project.id, summaries[0].paper_id)) == 5

    linked = await document_repository.get(
        project.id,
        (await evidence_repository.get(project.id, first_evidence_id)).document_id,
    )
    compacted = await ContextCompactor(service, evidence_repository).compact(
        summaries[0], linked.document_id
    )
    assert compacted.source_characters > 0
    assert len(compacted.evidence_index) == 5

    with pytest.raises(EvidenceReferencedError):
        await evidence_repository.delete(project.id, first_evidence_id)
    with pytest.raises(RecordNotFoundError):
        await evidence_repository.get("another-project", first_evidence_id)

    first_node = await evidence_repository.get(project.id, first_evidence_id)
    parsed_path = workspace.resolve_safe_path(
        project.id, f"parsed/{first_node.document_id}/document.json"
    )
    parsed_path.write_text(
        parsed_path.read_text(encoding="utf-8").replace(first_node.excerpt, "tampered text"),
        encoding="utf-8",
    )
    with pytest.raises(DocumentValidationError):
        EvidenceVerifier(service).verify(first_node)
