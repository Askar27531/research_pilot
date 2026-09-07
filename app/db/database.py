"""SQLite persistence layer for ResearchPilot.

Layered table map (single database file, see settings.database_path):

Domain data (user-visible research state)
  projects / research_profiles / project_analysis_profiles  课题与画像
  search_sessions / search_result_papers / papers           检索会话与命中论文
  paper_selections / paper_acquisitions / documents         选文、全文获取与登记
  evidence / visual_regions / evidence_reviews              证据、视觉区域与复核
  paper_summaries / summary_evidence_refs                   逐篇摘要及其证据引用
  selected_paper_analyses / analysis_reports                逐篇分析与综合报告
  document_analyses                                         每篇 PDF 解析/图表分析管线

Reliability data (worker scheduling & idempotency)
  workflow_jobs      后台任务队列（queued/running/succeeded/failed），部分唯一索引
                     保证同一项目同时只有一个活动任务
  work_items         细粒度工作项 + input_hash（同参不重算，失败重试不重复消耗 token）
  paper_acquisitions 同时承担全文获取状态机（pending/.../parsed/failed）
  traces             全流程事件/审计日志

LangGraph runtime state (NOT created by migrations)
  checkpoints / writes  由 AsyncSqliteSaver 在应用启动时创建（app/main.py）。thread_id
                        格式为 "{project_id}:search-{revision}:{track}"（见
                        app/agents/literature.py），因此可按 thread_id 前缀归属到项目；
                        ProjectRepository.delete 会在删除项目时一并清理这两个表的行，
                        避免孤儿 checkpoint 随项目增删无限累积。这两个表名由
                        langgraph-checkpoint-sqlite 默认生成，属保留名，业务建表应避开。
                        二者与应用数据同库，但无外键关联 projects。

Status-machine notes (each owns its own transitions; do not couple them casually)
  projects.status          created/running/waiting/completed/failed（顶层课题状态）
  workflow_jobs.status     queued/running/succeeded/failed（任务层，重启认领依据）
  document_analyses.status queued/running/completed/failed（单篇 PDF 分析管线）
  paper_acquisitions.status pending/downloading/awaiting_upload/parsed/failed
  work_items.status        running/completed/failed（幂等工作项）
  evidence_reviews.status  unreviewed/confirmed/doubted/excluded（证据复核）
  project_analysis_profiles.mode  legacy/paper_assistant_v2（历史分析模式兼容开关）

  document_analyses.total_visuals/completed_visuals/current_step 是 V10/V11 加入的
  视觉进度计数：total_visuals 在启动分析时置为区域总数，completed_visuals 从 0 随
  单个区域成功递增，current_step 记录最近进度文本。

Evidence vs visual_regions boundary
  visual_regions 是 document 解析管线的产物（某 PDF 页上的 figure/table 裁剪区域，
  含 bbox/source_hash）；evidence 是分析后挑出、可被引用与复核的证据（text/figure/
  table 三型），figure/table 型 evidence 通过 locator 指向 visual_regions 语义位置。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 17

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

MIGRATION_V12 = """
-- Scope cleanup: drop tables orphaned by product-scope cuts (V3/V4/V6/V7/V8 era).
-- Verified (repo-wide grep) that no app/test/ui/mcp code reads or writes them.
DROP TABLE IF EXISTS proposal_decisions;
DROP TABLE IF EXISTS experiment_proposals;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS method_cards;
DROP TABLE IF EXISTS transfer_candidate_sets;
DROP TABLE IF EXISTS conversation_messages;
DROP TABLE IF EXISTS revision_previews;
DROP TABLE IF EXISTS paper_sources;

-- Fix V11 backfill over-counting: legacy rows whose status was 'failed' were marked
-- completed_visuals=total_visuals even though their visuals never finished. V11 is the
-- only writer of current_step='图表分析失败' (runtime failures never set that literal),
-- so restricting on it keeps the update scoped to rows V11 itself touched.
UPDATE document_analyses
SET completed_visuals = 0
WHERE status = 'failed'
  AND total_visuals > 0
  AND completed_visuals = total_visuals
  AND current_step = '图表分析失败';
"""

MIGRATION_V13 = """
-- Decision-desk provenance (HITL lean plan): who decided each evidence review and
-- in which human review session. Legacy rows are backfilled from the note prefixes
-- the automatic passes have always written (自动复核：= visual verifier,
-- 图文一致：= cross-modal consistency); everything else is treated as human.
ALTER TABLE evidence_reviews ADD COLUMN source TEXT;
ALTER TABLE evidence_reviews ADD COLUMN review_session_id TEXT;
UPDATE evidence_reviews SET source = CASE
    WHEN note LIKE '自动复核：%' THEN 'auto_visual_verifier'
    WHEN note LIKE '图文一致：%' THEN 'auto_consistency'
    ELSE 'human'
END WHERE source IS NULL;
"""

MIGRATION_V14 = """
-- Analysis supervision (human-in-the-loop on the running analysis):
--  1. projects.pause_requested  - cooperative "pause at next safe boundary".
--  2. analysis_progress         - structured per-(paper, stage) board so the UI
--     can show exactly where a multi-stage analysis is at any moment.
--  3. analysis_part_instructions- per-(paper, part) extra requirements used by
--     "re-analyze one part" (part granularity = 4 specialist blocks + overview).
ALTER TABLE projects ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS analysis_progress (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped')),
    label TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, paper_id, stage_key)
);
CREATE TABLE IF NOT EXISTS analysis_part_instructions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    paper_id TEXT NOT NULL,
    part_key TEXT NOT NULL CHECK(part_key IN (
        'problem','method','experiment','critical','overview'
    )),
    instruction TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, revision, paper_id, part_key)
);
"""

MIGRATION_V15 = """
-- M3 cost-threshold pause (HITL升级计划 方案三): 
--   1. projects.pause_reason      - why the project is parked (user / budget_gate),
--      so the UI can tell a budget pause apart from a manual pause.
--   2. llm_usage                  - append-only per-call ledger fed by the provider
--      usage sink (kind text/vision, tokens, latency). FK cascade on project delete.
--   3. budget_windows             - one row per project: the current allowance
--      window (baseline tokens/vision + since) and the open gate marker
--      (open=1 + snapshot_json = totals when the gate fired). "Continue" acks the
--      gate by re-baselining the window to the current totals.
ALTER TABLE projects ADD COLUMN pause_reason TEXT;
CREATE TABLE IF NOT EXISTS llm_usage (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    phase TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('text','vision')),
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_project_created
ON llm_usage(project_id, created_at);
CREATE TABLE IF NOT EXISTS budget_windows (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    baseline_tokens INTEGER NOT NULL DEFAULT 0,
    baseline_vision INTEGER NOT NULL DEFAULT 0,
    since TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    snapshot_json TEXT
);
"""

MIGRATION_V16 = """
-- Event-ized HITL, minimal cut (方案一 M2-lite): one open hitl_event parked with
-- a `waiting` project IS the derived `waiting_for_human` semantic (no sixth
-- top-level status, CHECK constraints and `status in {...}` branches untouched).
-- The worker refuses to start jobs for a project holding an open event, so only
-- a human decision (resolve via an action) can move it forward.
CREATE TABLE IF NOT EXISTS hitl_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT,
    scope_json TEXT,
    options_json TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('blocking','escalation','info')),
    status TEXT NOT NULL CHECK(status IN ('open','resolved','cancelled','superseded')),
    created_by TEXT NOT NULL,
    default_action TEXT,
    resolved_by TEXT,
    resolution_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hitl_events_open
ON hitl_events(project_id, status, created_at);
"""

MIGRATION_V17 = """
-- M3 ReviewGate (analysis_graph interrupt#2): the pre-synthesis evidence-review
-- decision a human made is business-table truth that the gate reads on re-entry
-- (same pattern as the selection living in paper_selections):
--   analysis_review_decision: NULL (undecided) | 'continue' (skip review, run
--   the synthesis as-is) | 'regenerate_after_review' (clear the per-paper
--   analyses so the graph re-runs them with the adjudicated evidence pool —
--   untouched parts stay cached via work_items).
ALTER TABLE projects ADD COLUMN analysis_review_decision TEXT;
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
            await self._apply(connection, 12, MIGRATION_V12)
            await self._apply(connection, 13, MIGRATION_V13)
            await self._apply(connection, 14, MIGRATION_V14)
            await self._apply(connection, 15, MIGRATION_V15)
            await self._apply(connection, 16, MIGRATION_V16)
            await self._apply(connection, 17, MIGRATION_V17)
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
