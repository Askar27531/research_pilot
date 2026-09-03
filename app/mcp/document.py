from app.mcp.registry import CapabilityRouter
from app.schemas import ParsedDocument


class DocumentCapabilityClient:
    """Stable paper port for the project workflow's existing document identity model."""

    def __init__(self, router: CapabilityRouter) -> None:
        self.router = router

    async def parse_document(
        self, project_id: str, document_id: str, trace_id: str | None = None
    ) -> ParsedDocument:
        value = await self.router.call(
            "paper.parse",
            {"project_id": project_id, "document_id": document_id},
            context={"project_id": project_id, "trace_id": trace_id},
        )
        return ParsedDocument.model_validate(value)
