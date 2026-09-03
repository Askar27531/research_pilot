from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.api.errors import (
    RequestIDMiddleware,
    document_error_handler,
    evidence_referenced_handler,
    http_error_handler,
    internal_error_handler,
    literature_error_handler,
    llm_error_handler,
    project_conflict_handler,
    record_not_found_handler,
    validation_error_handler,
)
from app.api.routes.core import router as core_router
from app.api.routes.health import router as health_router
from app.api.routes.mcp import router as mcp_router
from app.artifacts import ArtifactService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import ArtifactRepository, Database
from app.db.errors import EvidenceReferencedError, ProjectConflictError, RecordNotFoundError
from app.documents import DocumentError, DocumentService, PDFParser, WorkspaceManager
from app.literature.errors import LiteratureError
from app.llm import LLMError
from app.mcp import (
    ArtifactCapabilityClient,
    CapabilityRouter,
    DocumentCapabilityClient,
    LiteratureCapabilityClient,
    MCPRegistry,
)
from app.skills import SkillRegistry
from app.workflow_worker import WorkflowWorker
from mcp_servers.artifact import create_artifact_server
from mcp_servers.document import create_document_server
from mcp_servers.literature.server import mcp as literature_mcp


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings.database_path)
    await database.initialize()
    checkpoint_connection = await aiosqlite.connect(database.path)
    checkpointer = AsyncSqliteSaver(
        checkpoint_connection,
        serde=JsonPlusSerializer(allowed_msgpack_modules=[]),
    )
    await checkpointer.setup()
    skill_registry = SkillRegistry(settings.skills_root, max_bytes=settings.skill_max_bytes)
    skill_registry.discover()
    app.state.database = database
    app.state.checkpointer = checkpointer
    app.state.skill_registry = skill_registry
    workspace = WorkspaceManager(
        settings.workspace_root, max_document_bytes=settings.document_max_bytes
    )
    app.state.document_service = DocumentService(
        workspace, PDFParser(
            workspace,
            render_dpi=settings.pdf_render_dpi,
            ocr_languages=settings.ocr_languages,
        )
    )
    artifact_service = ArtifactService(workspace, ArtifactRepository(database))
    registry = MCPRegistry.load(settings.mcp_config_path, {
        "researchpilot-literature": literature_mcp,
        "researchpilot-document": create_document_server(app.state.document_service),
        "researchpilot-artifact": create_artifact_server(artifact_service),
    })
    await registry.discover()
    app.state.mcp_registry = registry
    app.state.capability_router = CapabilityRouter(registry)
    app.state.document_capabilities = DocumentCapabilityClient(app.state.capability_router)
    app.state.artifact_capabilities = ArtifactCapabilityClient(app.state.capability_router)
    literature = LiteratureCapabilityClient(app.state.capability_router)
    workflow_worker = WorkflowWorker(app, literature)
    app.state.workflow_worker = workflow_worker
    await workflow_worker.start()
    try:
        yield
    finally:
        await workflow_worker.close()
        await checkpoint_connection.close()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="ResearchPilot API",
        version="0.1.0",
        description="Local-first, evidence-traceable multimodal paper research assistant",
        lifespan=lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_exception_handler(LLMError, llm_error_handler)
    app.add_exception_handler(LiteratureError, literature_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RecordNotFoundError, record_not_found_handler)
    app.add_exception_handler(ProjectConflictError, project_conflict_handler)
    app.add_exception_handler(EvidenceReferencedError, evidence_referenced_handler)
    app.add_exception_handler(DocumentError, document_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.include_router(health_router)
    app.include_router(mcp_router)
    app.include_router(core_router)
    return app


app = create_app()
