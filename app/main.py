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
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.documents import DocumentError, create_document_service
from app.literature.errors import LiteratureError
from app.llm import LLMError
from app.mcp import (
    CapabilityRouter,
    DocumentCapabilityClient,
    LiteratureCapabilityClient,
    MCPRegistry,
)
from app.skills import SkillRegistry
from app.workflow_worker import WorkflowWorker
from mcp_servers.document import create_document_server
from mcp_servers.literature.server import mcp as literature_mcp


@asynccontextmanager
# Durable-Execution: 启动装配——按顺序备好业务库、LangGraph 档位、技能、文档子系统、
# MCP 能力网关与后台 worker 后 yield 开始接请求；停机按逆序收尾（先停 worker 再关连接）。
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # ① 业务库：建库 + 幂等迁移（schema 1..18，重启自动补缺）
    database = Database(settings.database_path)
    await database.initialize()

    # ② LangGraph 档位：同一数据库文件建 checkpoints/writes + WAL（重启按原 run 续跑的地基）
    checkpoint_connection = await aiosqlite.connect(database.path)
    checkpointer = AsyncSqliteSaver(
        checkpoint_connection,
        serde=JsonPlusSerializer(allowed_msgpack_modules=[]),
    )
    await checkpointer.setup()

    # ③ 技能注册表：启动只建元数据索引，正文按需加载
    skill_registry = SkillRegistry(settings.skills_root, max_bytes=settings.skill_max_bytes)
    skill_registry.discover()

    # ④ 文档子系统：工作区 + 解析器 + 门面一次组装（应用与 MCP 文档能力共享同一实例）
    document_service = create_document_service(settings)

    # ⑤ MCP 能力网关：声明 server + 启动体检（检索/文档 inprocess + 外部 arXiv）
    registry = MCPRegistry.load(settings.mcp_config_path, {
        "researchpilot-literature": literature_mcp,
        "researchpilot-document": create_document_server(document_service),
    })
    await registry.discover()
    capability_router = CapabilityRouter(registry)

    # ⑥ 后台 worker（单机持久化队列）
    workflow_worker = WorkflowWorker(app, LiteratureCapabilityClient(capability_router))

    # 挂载到 app.state：请求处理与图节点都从这里取依赖
    app.state.database = database
    app.state.checkpointer = checkpointer
    app.state.skill_registry = skill_registry
    app.state.document_service = document_service
    app.state.mcp_registry = registry
    app.state.capability_router = capability_router
    app.state.document_capabilities = DocumentCapabilityClient(capability_router)
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
    app.add_exception_handler(DocumentError, document_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.include_router(health_router)
    app.include_router(mcp_router)
    app.include_router(core_router)
    return app


app = create_app()
