from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pymupdf
from fastapi.testclient import TestClient

from app.api.dependencies import get_database, get_document_service
from app.db import Database, PaperRepository, ProjectRepository
from app.documents import DocumentService, PDFParser, WorkspaceManager
from app.main import app
from app.schemas import PaperMetadata, RankedPaper, ResearchRequest


def pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Introduction")
    page.insert_text((72, 100), "The method improves registration accuracy.")
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 160), False)
    pixmap.clear_with(0x336699)
    page.insert_image(pymupdf.Rect(100, 150, 400, 390), stream=pixmap.tobytes("png"))
    page.insert_text((100, 420), "Figure 1. Registration architecture.")
    page.insert_text((100, 460), "Table 1. Registration accuracy results.")
    output = BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def test_evidence_api_import_create_lookup_summary_and_comparison(tmp_path) -> None:
    database = Database(tmp_path / "api.db")
    workspace = WorkspaceManager(tmp_path / "workspaces")
    service = DocumentService(workspace, PDFParser(workspace, render_dpi=72))
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_document_service] = lambda: service
    try:
        with TestClient(app) as client:
            client.portal.call(database.initialize)
            project = client.portal.call(
                ProjectRepository(database).create,
                "Evidence API",
                ResearchRequest(research_question="Compare registration methods"),
            )
            paper = client.portal.call(
                PaperRepository(database).upsert_ranked,
                project.id,
                RankedPaper(
                    paper=PaperMetadata(
                        stable_id="paper-1", source_id="W1", title="Registration paper"
                    ),
                    lexical_score=0.8,
                    llm_score=0.9,
                    final_score=0.85,
                    selection_reason="Relevant",
                ),
            )
            imported = client.post(
                f"/projects/{project.id}/documents/import",
                data={"paper_id": paper.id},
                files={"file": ("paper.pdf", pdf_bytes(), "application/pdf")},
            )
            assert imported.status_code == 201
            document_id = imported.json()["document_id"]
            evidence = client.post(
                f"/projects/{project.id}/evidence/text",
                json={
                    "paper_id": paper.id,
                    "document_id": document_id,
                    "page_number": 1,
                    "claim": "The method improves registration accuracy.",
                    "quote": "The method improves registration accuracy.",
                    "confidence": 0.95,
                },
            )
            assert evidence.status_code == 201
            evidence_id = evidence.json()["evidence_id"]
            parsed = service.get_structure(project.id, document_id)
            figure_evidence = client.post(
                f"/projects/{project.id}/evidence/figure",
                json={
                    "paper_id": paper.id,
                    "document_id": document_id,
                    "figure_id": parsed.figures[0].figure_id,
                    "claim": "The paper presents a registration architecture.",
                    "confidence": 0.9,
                },
            )
            table_evidence = client.post(
                f"/projects/{project.id}/evidence/table",
                json={
                    "paper_id": paper.id,
                    "document_id": document_id,
                    "table_id": parsed.tables[0].table_id,
                    "claim": "The paper reports registration accuracy.",
                    "confidence": 0.9,
                },
            )
            source = client.get(f"/projects/{project.id}/evidence/{evidence_id}/source")
            listed = client.get(f"/projects/{project.id}/evidence", params={"paper_id": paper.id})
            summary = client.put(
                f"/projects/{project.id}/summaries/{paper.id}",
                json={
                    "summary_id": str(uuid4()),
                    "project_id": "ignored-by-path",
                    "paper_id": "ignored-by-path",
                    "method": [
                        {
                            "value": "The method improves registration accuracy.",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            comparison = client.get(f"/projects/{project.id}/comparison")
            isolated = client.get(f"/projects/another/evidence/{evidence_id}")
    finally:
        app.dependency_overrides.clear()

    assert source.status_code == 200
    assert source.json()["excerpt"] == "The method improves registration accuracy."
    assert figure_evidence.status_code == 201
    assert figure_evidence.json()["evidence_type"] == "figure"
    assert table_evidence.status_code == 201
    assert table_evidence.json()["evidence_type"] == "table"
    assert len(listed.json()) == 3
    assert summary.status_code == 200
    assert comparison.json()["rows"][0]["cells"]["method"]["evidence_ids"] == [evidence_id]
    assert isolated.status_code == 404
