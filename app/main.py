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
from app.api.routes.artifacts import router as artifacts_router
from app.api.routes.evidence import router as evidence_router
from app.api.routes.experiments import router as experiments_router
from app.api.routes.health import router as health_router
from app.api.routes.models import router as models_router
from app.api.routes.projects import router as projects_router
from app.api.routes.research import router as research_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import Database
from app.db.errors import EvidenceReferencedError, ProjectConflictError, RecordNotFoundError
from app.documents import DocumentError, DocumentService, PDFParser, WorkspaceManager
from app.literature.errors import LiteratureError
from app.llm import LLMError
from app.skills import SkillRegistry


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
        workspace, PDFParser(workspace, render_dpi=settings.document_render_dpi)
    )
    try:
        yield
    finally:
        await checkpoint_connection.close()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="ResearchPilot API",
        version="0.1.0",
        description="Skill-driven multimodal deep research agent",
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
    app.include_router(models_router)
    app.include_router(research_router)
    app.include_router(projects_router)
    app.include_router(evidence_router)
    app.include_router(experiments_router)
    app.include_router(artifacts_router)
    return app


app = create_app()
