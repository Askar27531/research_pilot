import pytest
from fastmcp import Client

from app.artifacts import ArtifactService
from app.db import ArtifactRepository, Database, ProjectRepository
from app.documents import WorkspaceManager
from app.schemas import ArtifactRecord, ResearchRequest
from mcp_servers.artifact import create_artifact_server


@pytest.mark.asyncio
async def test_artifact_tools_cross_mcp_boundary(tmp_path) -> None:
    database = Database(tmp_path / "artifacts.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "MCP artifacts", ResearchRequest(research_question="Generate artifacts")
    )
    service = ArtifactService(
        WorkspaceManager(tmp_path / "workspaces"), ArtifactRepository(database)
    )
    server = create_artifact_server(service)
    async with Client(server) as client:
        markdown = await client.call_tool(
            "create_markdown",
            {
                "project_id": project.id,
                "name": "plan.md",
                "title": "Plan",
                "sections": {"Goal": "Test"},
            },
        )
        csv_result = await client.call_tool(
            "create_csv",
            {
                "project_id": project.id,
                "name": "matrix.csv",
                "columns": ["method", "score"],
                "rows": [["A", "1"]],
            },
        )
        mermaid = await client.call_tool(
            "create_mermaid",
            {
                "project_id": project.id,
                "name": "pipeline.mmd",
                "diagram": "flowchart TD\nA-->B",
            },
        )
    assert ArtifactRecord.model_validate(markdown.structured_content).artifact_type == "markdown"
    assert ArtifactRecord.model_validate(csv_result.structured_content).artifact_type == "csv"
    assert ArtifactRecord.model_validate(mermaid.structured_content).artifact_type == "mermaid"
