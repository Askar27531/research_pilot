from app.mcp.registry import CapabilityRouter
from app.schemas import ArtifactRecord


class ArtifactCapabilityClient:
    """Stable artifact port; callers do not know MCP tool or server names."""

    def __init__(self, router: CapabilityRouter) -> None:
        self.router = router

    async def markdown(
        self, project_id: str, name: str, title: str, sections: dict[str, str]
    ) -> ArtifactRecord:
        value = await self.router.call("artifact.markdown", {
            "project_id": project_id, "name": name, "title": title, "sections": sections,
        }, context={"project_id": project_id})
        return ArtifactRecord.model_validate(value)

    async def csv(
        self, project_id: str, name: str, columns: list[str], rows: list[list[str]]
    ) -> ArtifactRecord:
        value = await self.router.call("artifact.csv", {
            "project_id": project_id, "name": name, "columns": columns, "rows": rows,
        }, context={"project_id": project_id})
        return ArtifactRecord.model_validate(value)

    async def mermaid(self, project_id: str, name: str, diagram: str) -> ArtifactRecord:
        value = await self.router.call("artifact.mermaid", {
            "project_id": project_id, "name": name, "diagram": diagram,
        }, context={"project_id": project_id})
        return ArtifactRecord.model_validate(value)
