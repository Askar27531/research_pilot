from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Request

from app.db import (
    ArtifactRepository,
    Database,
    DocumentRepository,
    EvidenceRepository,
    PaperRepository,
    ProjectRepository,
    ProposalRepository,
    SummaryRepository,
    TraceRepository,
    WorkItemRepository,
)
from app.documents import DocumentService
from app.literature import LiteratureMCPClient
from app.llm import LLMProvider, OllamaProvider
from app.skills import SkillRegistry
from mcp_servers.literature.server import mcp as literature_mcp


async def get_llm_provider() -> AsyncIterator[LLMProvider]:
    """Provide one managed LLM client per request."""

    async with OllamaProvider() as provider:
        yield provider


def get_checkpointer(request: Request) -> Any:
    return request.app.state.checkpointer


def get_skill_registry(request: Request) -> SkillRegistry:
    return request.app.state.skill_registry


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_document_service(request: Request) -> DocumentService:
    return request.app.state.document_service


def get_project_repository(
    database: Annotated[Database, Depends(get_database)],
) -> ProjectRepository:
    return ProjectRepository(database)


def get_paper_repository(
    database: Annotated[Database, Depends(get_database)],
) -> PaperRepository:
    return PaperRepository(database)


def get_trace_repository(
    database: Annotated[Database, Depends(get_database)],
) -> TraceRepository:
    return TraceRepository(database)


def get_document_repository(
    database: Annotated[Database, Depends(get_database)],
) -> DocumentRepository:
    return DocumentRepository(database)


def get_evidence_repository(
    database: Annotated[Database, Depends(get_database)],
) -> EvidenceRepository:
    return EvidenceRepository(database)


def get_summary_repository(
    database: Annotated[Database, Depends(get_database)],
) -> SummaryRepository:
    return SummaryRepository(database)


def get_proposal_repository(
    database: Annotated[Database, Depends(get_database)],
) -> ProposalRepository:
    return ProposalRepository(database)


def get_artifact_repository(
    database: Annotated[Database, Depends(get_database)],
) -> ArtifactRepository:
    return ArtifactRepository(database)


def get_work_item_repository(
    database: Annotated[Database, Depends(get_database)],
) -> WorkItemRepository:
    return WorkItemRepository(database)


@lru_cache
def get_literature_client() -> LiteratureMCPClient:
    return LiteratureMCPClient(literature_mcp)
