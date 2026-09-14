from app.mcp.registry import CapabilityRouter
from app.schemas import ParsedDocument


class DocumentCapabilityClient:
    """Stable paper port for the project workflow's existing document identity model."""

    def __init__(self, router: CapabilityRouter) -> None:
        self.router = router

    async def parse_document(self, project_id: str, document_id: str) -> ParsedDocument:
        value = await self.router.call(
            "paper.parse",
            {"project_id": project_id, "document_id": document_id},
        )
        return ParsedDocument.model_validate(value)
