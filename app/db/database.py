from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 11

MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('created','running','waiting','completed','failed')),
    current_stage TEXT NOT NULL,
    last_run_id TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    lexical_score REAL,
    llm_score REAL,
    relevance_score REAL,
    selection_reason TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, stable_key)
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    agent TEXT,
    node TEXT,
    tool TEXT,
    success INTEGER NOT NULL,
    latency_ms INTEGER,
    summary_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_papers_project_score
ON papers(project_id, relevance_score DESC);

CREATE INDEX IF NOT EXISTS idx_traces_project_created
ON traces(project_id, created_at);
"""

MIGRATION_V2 = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, paper_id),
    UNIQUE(project_id, sha256)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('text','figure','table')),
    locator_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, paper_id, locator_key)
);

CREATE TABLE IF NOT EXISTS paper_summaries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, paper_id)
);

CREATE TABLE IF NOT EXISTS summary_evidence_refs (
    summary_id TEXT NOT NULL REFERENCES paper_summaries(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
    PRIMARY KEY(summary_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_project_paper
ON evidence(project_id, paper_id, created_at);
"""

MIGRATION_V3 = """
CREATE TABLE IF NOT EXISTS experiment_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','modified','rejected')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id)
);

CREATE TABLE IF NOT EXISTS proposal_decisions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES experiment_proposals(id) ON DELETE CASCADE,
    from_version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('accept','modify','reject')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MIGRATION_V4 = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('markdown','csv','mermaid')),
    name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project_name
ON artifacts(project_id, name, version DESC);
"""

MIGRATION_V5 = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_scope TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    attempts INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, run_scope, item_key)
);
CREATE INDEX IF NOT EXISTS idx_work_items_project_status
ON work_items(project_id, status, updated_at);
"""

MIGRATION_V6 = """
CREATE TABLE IF NOT EXISTS research_profiles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS method_cards (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, paper_id)
);
CREATE TABLE IF NOT EXISTS transfer_candidate_sets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','modified','rejected')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    context TEXT NOT NULL CHECK(context IN ('profile','transfer','proposal')),
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_acquisitions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending','downloading','awaiting_upload','parsed','failed')),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_method_cards_project ON method_cards(project_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_project ON conversation_messages(project_id, created_at);
"""

MIGRATION_V7 = """
CREATE TABLE IF NOT EXISTS workflow_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_jobs_active_project
ON workflow_jobs(project_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_queue
ON workflow_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS revision_previews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target TEXT NOT NULL CHECK(target IN ('transfer','experiment')),
    base_version INTEGER NOT NULL,
    instruction TEXT NOT NULL,
    summary TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','applied','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revision_previews_project
ON revision_previews(project_id, target, created_at DESC);
"""

MIGRATION_V8 = """
CREATE TABLE IF NOT EXISTS project_analysis_profiles (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('legacy','paper_assistant_v2')),
    outputs_stale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO project_analysis_profiles(project_id,mode,created_at,updated_at)
SELECT id,'legacy',created_at,updated_at FROM projects;

CREATE TABLE IF NOT EXISTS literature_searches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    warnings_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_sources (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('openalex','crossref','arxiv')),
    source_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,paper_id,source,source_id)
);
CREATE TABLE IF NOT EXISTS document_analyses (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    pipeline_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
    vision_model TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(document_id,pipeline_version,input_hash)
);
CREATE TABLE IF NOT EXISTS visual_regions (
    id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES document_analyses(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    region_type TEXT NOT NULL CHECK(region_type IN ('figure','table')),
    page_number INTEGER NOT NULL,
    label TEXT,
    bbox_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_reviews (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('unreviewed','confirmed','doubted','excluded')),
    note TEXT,
    updated_at TEXT NOT NULL
);
ALTER TABLE workflow_jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'research'
    CHECK(job_type IN ('research','document_analysis','resynthesis'));
CREATE INDEX IF NOT EXISTS idx_literature_searches_project
ON literature_searches(project_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visual_regions_document
ON visual_regions(project_id,document_id,page_number);
"""

MIGRATION_V9 = """
CREATE TABLE IF NOT EXISTS search_sessions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    instruction TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','completed','failed')),
    plan_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,revision)
);
CREATE TABLE IF NOT EXISTS search_result_papers (
    project_id TEXT NOT NULL,
    search_revision INTEGER NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    PRIMARY KEY(project_id,search_revision,paper_id),
    FOREIGN KEY(project_id,search_revision)
        REFERENCES search_sessions(project_id,revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS paper_selections (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    search_revision INTEGER NOT NULL,
    paper_ids_json TEXT NOT NULL,
    requirements TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selected_paper_analyses (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    search_revision INTEGER NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,search_revision,paper_id)
);
CREATE TABLE IF NOT EXISTS analysis_reports (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    search_revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,search_revision)
);
"""

MIGRATION_V10 = """
ALTER TABLE document_analyses ADD COLUMN total_visuals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE document_analyses ADD COLUMN completed_visuals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE document_analyses ADD COLUMN current_step TEXT;
"""

MIGRATION_V11 = """
UPDATE document_analyses
SET total_visuals=(SELECT COUNT(*) FROM visual_regions WHERE analysis_id=document_analyses.id),
    completed_visuals=(SELECT COUNT(*) FROM visual_regions WHERE analysis_id=document_analyses.id),
    current_step=CASE
        WHEN status='completed' THEN '图表分析已完成'
        WHEN status='failed' THEN '图表分析失败'
        ELSE current_step
    END
WHERE total_visuals=0
  AND EXISTS (SELECT 1 FROM visual_regions WHERE analysis_id=document_analyses.id);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.executescript(MIGRATION_V1)
            await self._apply(connection, 1, "")
            await self._apply(connection, 2, MIGRATION_V2)
            await self._apply(connection, 3, MIGRATION_V3)
            await self._apply(connection, 4, MIGRATION_V4)
            await self._apply(connection, 5, MIGRATION_V5)
            await self._apply(connection, 6, MIGRATION_V6)
            await self._apply(connection, 7, MIGRATION_V7)
            await self._apply(connection, 8, MIGRATION_V8)
            await self._apply(connection, 9, MIGRATION_V9)
            await self._apply(connection, 10, MIGRATION_V10)
            await self._apply(connection, 11, MIGRATION_V11)
            await connection.commit()

    @staticmethod
    async def _apply(connection: aiosqlite.Connection, version: int, sql: str) -> None:
        row = await (
            await connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
        ).fetchone()
        if row is not None:
            return
        if sql:
            await connection.executescript(sql)
        await connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (version,),
        )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            await connection.close()
