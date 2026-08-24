from fastmcp import FastMCP

from app.artifacts import ArtifactService
from app.schemas import ArtifactRecord


def create_artifact_server(service: ArtifactService) -> FastMCP:
    server = FastMCP("ResearchPilot Artifact")

    @server.tool
    async def create_markdown(
        project_id: str, name: str, title: str, sections: dict[str, str]
    ) -> ArtifactRecord:
        return await service.markdown(project_id, name, title, sections)

    @server.tool
    async def create_csv(
        project_id: str, name: str, columns: list[str], rows: list[list[str]]
    ) -> ArtifactRecord:
        return await service.csv(project_id, name, columns, rows)

    @server.tool
    async def create_mermaid(project_id: str, name: str, diagram: str) -> ArtifactRecord:
        return await service.mermaid(project_id, name, diagram)

    return server
