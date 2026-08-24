from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 5

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
