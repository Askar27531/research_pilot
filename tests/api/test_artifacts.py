from fastapi.testclient import TestClient

from app.api.dependencies import get_database, get_document_service
from app.db import Database, ProjectRepository
from app.documents import DocumentService, PDFParser, WorkspaceManager
from app.main import app
from app.schemas import ResearchRequest


def test_artifact_generation_versioning_download_and_safety(tmp_path) -> None:
    database = Database(tmp_path / "artifacts.db")
    workspace = WorkspaceManager(tmp_path / "workspaces")
    documents = DocumentService(workspace, PDFParser(workspace))
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_document_service] = lambda: documents
    try:
        with TestClient(app) as client:
            client.portal.call(database.initialize)
            project = client.portal.call(
                ProjectRepository(database).create,
                "Artifacts",
                ResearchRequest(research_question="Build artifacts"),
            )
            first = client.post(
                f"/projects/{project.id}/artifacts/markdown",
                json={"name": "plan.md", "title": "Plan", "sections": {"Goal": "Test"}},
            )
            second = client.post(
                f"/projects/{project.id}/artifacts/markdown",
                json={"name": "plan.md", "title": "Plan v2", "sections": {"Goal": "Test again"}},
            )
            csv_result = client.post(
                f"/projects/{project.id}/artifacts/csv",
                json={"name": "matrix.csv", "columns": ["method", "score"], "rows": [["A", "1"]]},
            )
            mermaid = client.post(
                f"/projects/{project.id}/artifacts/mermaid",
                json={"name": "pipeline.mmd", "diagram": "flowchart TD\nA-->B"},
            )
            unsafe = client.post(
                f"/projects/{project.id}/artifacts/mermaid",
                json={"name": "bad.mmd", "diagram": "flowchart TD\nclick A javascript:alert(1)"},
            )
            listed = client.get(f"/projects/{project.id}/artifacts")
            download = client.get(
                f"/projects/{project.id}/artifacts/{first.json()['artifact_id']}/download"
            )
    finally:
        app.dependency_overrides.clear()
    assert first.json()["version"] == 1
    assert second.json()["version"] == 2
    assert csv_result.status_code == 201 and mermaid.status_code == 201
    assert unsafe.status_code == 400
    assert len(listed.json()) == 4
    assert download.content.startswith(b"# Plan")
