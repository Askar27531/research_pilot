from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies import get_database, get_workspace_service
from app.api.tokens import issue_token, read_token
from app.db import Database
from app.documents import DocumentSecurityError
from app.main import app
from app.services.workspace import WorkspaceResource


async def ready_health(_request):
    return SimpleNamespace(paper_analysis=SimpleNamespace(ready=True, blockers=[]))


def test_openapi_exposes_only_core_workspace_paths() -> None:
    with TestClient(app) as client:
        paths = set(client.get("/openapi.json").json()["paths"])
    assert paths == {
        "/health", "/mcp/status", "/projects", "/projects/{project_id}",
        "/projects/{project_id}/workspace",
        "/projects/{project_id}/actions", "/projects/{project_id}/documents",
        "/projects/{project_id}/resources/{token}",
    }


def test_create_project_is_atomic_workspace_operation(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "core.db")
    monkeypatch.setattr("app.api.routes.core.health", ready_health)
    app.dependency_overrides[get_database] = lambda: database
    try:
        with TestClient(app) as client:
            client.portal.call(database.initialize)
            response = client.post("/projects", json={
                "research_question": "How can multimodal paper evidence improve registration?",
                "current_approach": "Feature matching",
                "difficulties": ["Low-light robustness"],
                "target_metrics": ["Registration error"],
                "advanced": {"year_from": 2022, "year_to": 2026, "max_papers": 10,
                             "sources": ["openalex", "crossref", "arxiv"]},
            })
            assert response.status_code == 202
            payload = response.json()
            project_id = payload["workspace"]["project_id"]
            assert payload["job"]["job_type"] == "research"
            assert client.get("/projects").json()[0]["id"] == project_id
            assert client.get(f"/projects/{project_id}/workspace").status_code == 200
            assert client.post(f"/projects/{project_id}/resume", json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_resource_tokens_are_bound_to_project() -> None:
    token = issue_token("project-a", "artifact", "artifact-1")
    assert read_token(token, "project-a", "artifact")["i"] == "artifact-1"
    try:
        read_token(token, "project-b", "artifact")
    except DocumentSecurityError:
        pass
    else:
        raise AssertionError("cross-project resource token was accepted")


def test_removed_routes_are_not_exposed() -> None:
    removed = [
        ("POST", "/models/test"),
        ("POST", "/research/test"),
        ("POST", "/projects/missing/research"),
        ("POST", "/projects/missing/resume"),
        ("GET", "/projects/missing/evidence"),
        ("GET", "/projects/missing/artifacts"),
    ]
    with TestClient(app) as client:
        for method, path in removed:
            assert client.request(method, path, json={}).status_code == 404


def test_core_mutations_delegate_to_workspace_service() -> None:
    workspace = {
        "project_id": "project-1", "name": "Paper review", "user_stage": "searching",
        "status_label": "Running", "status_detail": "Working",
        "next_action": {"type": "wait"}, "progress": {},
    }

    class Service:
        max_upload_bytes = 100

        def __init__(self):
            self.action_type = None
            self.uploaded = None
            self.deleted = None

        async def action(self, project_id, action):
            self.action_type = (project_id, action.type)
            return {"message": "queued", "workspace": workspace}

        async def upload(self, project_id, token, filename, content):
            self.uploaded = (project_id, token, filename, content)
            return {"message": "uploaded", "workspace": workspace}

        async def resource(self, project_id, token):
            assert (project_id, token) == ("project-1", "resource-token")
            return WorkspaceResource(b"report", "text/markdown", "report.md")

        async def delete(self, project_id):
            self.deleted = project_id

    service = Service()
    app.dependency_overrides[get_workspace_service] = lambda: service
    try:
        with TestClient(app) as client:
            action = client.post(
                "/projects/project-1/actions", json={"type": "run"}
            )
            upload = client.post(
                "/projects/project-1/documents",
                data={"upload_token": "opaque-upload-token"},
                files={"files": ("paper.pdf", b"%PDF-demo", "application/pdf")},
            )
            resource = client.get("/projects/project-1/resources/resource-token")
            deleted = client.delete("/projects/project-1")
        assert action.status_code == 200
        assert service.action_type == ("project-1", "run")
        assert upload.status_code == 202
        assert service.uploaded == (
            "project-1", "opaque-upload-token", "paper.pdf", b"%PDF-demo"
        )
        assert resource.content == b"report"
        assert 'filename="report.md"' in resource.headers["content-disposition"]
        assert deleted.status_code == 204
        assert service.deleted == "project-1"
    finally:
        app.dependency_overrides.clear()
