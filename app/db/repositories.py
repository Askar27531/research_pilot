import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.literature.deduplication import paper_key
from app.schemas import (
    ProjectRecord,
    RankedPaper,
    ResearchRequest,
    StoredPaper,
    TraceMetrics,
    TraceRecord,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(value: str | None) -> Any:
    return json.loads(value) if value else None


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        name: str,
        request: ResearchRequest,
        project_id: str | None = None,
    ) -> ProjectRecord:
        identifier = project_id or str(uuid4())
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO projects(
                    id, name, goal, request_json, status, current_stage,
                    created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, 'created', 'initialized', ?, ?, 1)
                """,
                (
                    identifier,
                    name.strip(),
                    request.research_question,
                    request.model_dump_json(),
                    now,
                    now,
                ),
            )
            await connection.commit()
        return await self.get(identifier)

    async def get(self, project_id: str) -> ProjectRecord:
        async with self.database.connect() as connection:
            row = await self._fetch(connection, project_id)
        return self._to_record(row)

    async def list(self, *, limit: int = 100, offset: int = 0) -> list[ProjectRecord]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (limit, offset),
            )).fetchall()
        return [self._to_record(row) for row in rows]

    async def delete(self, project_id: str) -> None:
        """Delete a project and all database records linked through foreign keys."""
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self._fetch(connection, project_id)
            active = await (await connection.execute(
                "SELECT 1 FROM workflow_jobs WHERE project_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (project_id,),
            )).fetchone()
            if active is not None:
                await connection.rollback()
                raise ProjectConflictError("Project still has an active workflow job")
            # LangGraph checkpoint tables are created by AsyncSqliteSaver at app startup
            # (app/main.py), not by migrations, so they may be absent (e.g. unit tests).
            # Their thread_id is "{project_id}:search-{revision}:{track}"
            # (see app/agents/literature.py), so a prefix delete removes every graph run
            # this project ever started; otherwise orphan rows would accumulate forever.
            owns_graph_state = await (await connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('checkpoints','writes')"
            )).fetchone()
            if owns_graph_state[0] == 2:
                prefix = f"{project_id}:%"
                await connection.execute(
                    "DELETE FROM checkpoints WHERE thread_id LIKE ?", (prefix,)
                )
                await connection.execute(
                    "DELETE FROM writes WHERE thread_id LIKE ?", (prefix,)
                )
            await connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
            await connection.commit()

    async def update_definition(
        self, project_id: str, name: str, request: ResearchRequest
    ) -> ProjectRecord:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET name=?,goal=?,request_json=?,updated_at=?,version=version+1 "
                "WHERE id=?",
                (name.strip(), request.research_question, request.model_dump_json(),
                 utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def start_run(
        self,
        project_id: str,
        run_id: str,
        *,
        resume: bool = False,
    ) -> tuple[ProjectRecord, bool]:
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await self._fetch(connection, project_id)
            status = row["status"]
            if status == "completed":
                await connection.rollback()
                return self._to_record(row), False
            if status == "running":
                await connection.rollback()
                if row["last_run_id"] == run_id:
                    return self._to_record(row), False
                raise ProjectConflictError("Project research is already running")
            if resume and status not in {"failed", "waiting"}:
                await connection.rollback()
                raise ProjectConflictError("Only failed or waiting projects can be resumed")
            if not resume and status == "failed":
                await connection.rollback()
                raise ProjectConflictError("Failed projects must use the resume endpoint")
            now = utc_now()
            await connection.execute(
                """
                UPDATE projects
                SET status='running', current_stage='starting', last_run_id=?,
                    error_json=NULL, updated_at=?, version=version+1
                WHERE id=?
                """,
                (run_id, now, project_id),
            )
            await connection.commit()
        return await self.get(project_id), True

    async def complete(self, project_id: str, current_stage: str) -> ProjectRecord:
        return await self._set_terminal(project_id, "completed", current_stage, None)

    async def wait(self, project_id: str, current_stage: str) -> ProjectRecord:
        return await self._set_terminal(project_id, "waiting", current_stage, None)

    async def fail(
        self, project_id: str, current_stage: str, error: dict[str, Any]
    ) -> ProjectRecord:
        return await self._set_terminal(project_id, "failed", current_stage, error)

    async def set_stage(self, project_id: str, current_stage: str) -> ProjectRecord:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET current_stage=?,updated_at=?,version=version+1 WHERE id=?",
                (current_stage, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def reopen(self, project_id: str, current_stage: str) -> ProjectRecord:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET status='waiting',current_stage=?,error_json=NULL,updated_at=?,"
                "version=version+1 WHERE id=?", (current_stage, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def wait_for_human(
        self,
        project_id: str,
        current_stage: str,
        *,
        event_type: str,
        title: str,
        reason: str | None = None,
        scope: dict[str, Any] | None = None,
        options: "list[dict[str, str]] | None" = None,
        level: str = "blocking",
        created_by: str = "system",
        default_action: str | None = None,
        run_id: str | None = None,
    ) -> ProjectRecord:
        """Park a project at *current_stage* AND record one open hitl_event, in a
        single transaction.

        ``status='waiting'`` + an open event is the derived ``waiting_for_human``
        semantic (the plan deliberately avoids a sixth top-level status). The
        worker refuses to start jobs for a project holding an open event, so a
        human decision (resolving the event via an action) is the only way the
        project moves forward again.
        """
        event_id = str(uuid4())
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "UPDATE projects SET status='waiting',current_stage=?,error_json=NULL,"
                "updated_at=?,version=version+1 WHERE id=?",
                (current_stage, now, project_id),
            )
            if cursor.rowcount == 0:
                await connection.rollback()
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.execute(
                "INSERT INTO hitl_events(id,project_id,run_id,type,title,reason,"
                "scope_json,options_json,level,status,created_by,default_action,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'open',?,?,?)",
                (event_id, project_id, run_id, event_type, title, reason,
                 dump_json(scope or {}), dump_json(options or []), level,
                 created_by, default_action, now),
            )
            await connection.commit()
        return await self.get(project_id)

    async def set_pause_requested(self, project_id: str, requested: bool) -> ProjectRecord:
        """Cooperative pause flag consumed by the analysis worker at safe boundaries."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET pause_requested=?,updated_at=?,version=version+1 WHERE id=?",
                (1 if requested else 0, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def set_pause_reason(self, project_id: str, reason: str | None) -> ProjectRecord:
        """Record why the project is parked (``user`` / ``budget_gate`` / None)."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET pause_reason=?,updated_at=?,version=version+1 WHERE id=?",
                (reason, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def set_analysis_review_decision(
        self, project_id: str, decision: str | None
    ) -> ProjectRecord:
        """Record / clear the M3 ReviewGate decision (None | continue |
        regenerate_after_review). The gate node reads this business-table truth
        on every entry so human decisions survive job re-runs."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE projects SET analysis_review_decision=?,updated_at=?,"
                "version=version+1 WHERE id=?",
                (decision, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def _set_terminal(
        self,
        project_id: str,
        status: str,
        current_stage: str,
        error: dict[str, Any] | None,
    ) -> ProjectRecord:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE projects
                SET status=?, current_stage=?, error_json=?, updated_at=?, version=version+1
                WHERE id=?
                """,
                (status, current_stage, dump_json(error) if error else None, utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                await connection.rollback()
                raise RecordNotFoundError(f"Project not found: {project_id}")
            await connection.commit()
        return await self.get(project_id)

    async def _fetch(self, connection: aiosqlite.Connection, project_id: str) -> aiosqlite.Row:
        cursor = await connection.execute("SELECT * FROM projects WHERE id=?", (project_id,))
        row = await cursor.fetchone()
        if row is None:
            raise RecordNotFoundError(f"Project not found: {project_id}")
        return row

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> ProjectRecord:
        columns = row.keys()
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            goal=row["goal"],
            request=ResearchRequest.model_validate_json(row["request_json"]),
            status=row["status"],
            current_stage=row["current_stage"],
            last_run_id=row["last_run_id"],
            error=load_json(row["error_json"]),
            pause_reason=row["pause_reason"] if "pause_reason" in columns else None,
            analysis_review_decision=(
                row["analysis_review_decision"]
                if "analysis_review_decision" in columns else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
        )


class HitlEventRepository:
    """Read / resolve ``hitl_events``.

    Creation is deliberately atomic with the project park
    (:meth:`ProjectRepository.wait_for_human`), so a project can never sit at
    ``waiting`` *without* its event or hold an event while still ``running``.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    async def open_events(self, project_id: str) -> list[dict[str, Any]]:
        """Every open (awaiting-human) event of a project, oldest first."""
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT * FROM hitl_events WHERE project_id=? AND status='open' "
                "ORDER BY created_at, id", (project_id,),
            )).fetchall()
        return [{
            "id": row["id"], "type": row["type"], "title": row["title"],
            "reason": row["reason"], "scope": load_json(row["scope_json"]),
            "options": load_json(row["options_json"]), "level": row["level"],
            "created_by": row["created_by"], "default_action": row["default_action"],
            "run_id": row["run_id"], "created_at": row["created_at"],
        } for row in rows]

    async def has_open(self, project_id: str) -> bool:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM hitl_events WHERE project_id=? AND status='open' LIMIT 1",
                (project_id,),
            )).fetchone()
        return row is not None

    async def resolve_all(
        self,
        project_id: str,
        *,
        event_type: str | None = None,
        resolved_by: str = "action",
        resolution: dict[str, Any] | None = None,
        status: str = "resolved",
    ) -> int:
        """Close every matching open event (a human action consumed the wait).

        ``status`` may be ``resolved`` (the human decided and the project moved
        on) or ``superseded`` (the wait became obsolete, e.g. a new search
        replaced the pending paper selection). Returns the closed count.
        """
        clause = " AND type=?" if event_type is not None else ""
        now = utc_now()
        params: list[Any] = [status, resolved_by,
                             dump_json(resolution) if resolution else None, now]
        params.append(project_id)
        if event_type is not None:
            params.append(event_type)
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                f"UPDATE hitl_events SET status=?,resolved_by=?,resolution_json=?,"
                f"resolved_at=? WHERE project_id=? AND status='open'{clause}",
                params,
            )
            await connection.commit()
        return cursor.rowcount


class PaperRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_ranked(
        self, project_id: str, ranked: RankedPaper, *, selected: bool = False
    ) -> StoredPaper:
        key = paper_key(ranked.paper)
        now = utc_now()
        identifier = str(uuid4())
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO papers(
                    id, project_id, stable_key, metadata_json, lexical_score,
                    llm_score, relevance_score, selection_reason, selected,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, stable_key) DO UPDATE SET
                    metadata_json=excluded.metadata_json,
                    lexical_score=excluded.lexical_score,
                    llm_score=excluded.llm_score,
                    relevance_score=excluded.relevance_score,
                    selection_reason=excluded.selection_reason,
                    selected=excluded.selected,
                    updated_at=excluded.updated_at
                """,
                (
                    identifier,
                    project_id,
                    key,
                    ranked.paper.model_dump_json(),
                    ranked.lexical_score,
                    ranked.llm_score,
                    ranked.final_score,
                    ranked.selection_reason,
                    int(selected),
                    now,
                    now,
                ),
            )
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM papers WHERE project_id=? AND stable_key=?",
                (project_id, key),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Paper upsert did not return a row")
        return self._to_record(row)

    async def list_for_project(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[StoredPaper]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM papers WHERE project_id=?
                ORDER BY relevance_score DESC, stable_key ASC
                LIMIT ? OFFSET ?
                """,
                (project_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [self._to_record(row) for row in rows]

    async def get(self, project_id: str, paper_id: str) -> StoredPaper:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM papers WHERE project_id=? AND id=?", (project_id, paper_id)
            )).fetchone()
        if row is None:
            raise RecordNotFoundError("Paper not found")
        return self._to_record(row)

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> StoredPaper:
        return StoredPaper(
            id=row["id"],
            project_id=row["project_id"],
            stable_key=row["stable_key"],
            metadata=load_json(row["metadata_json"]),
            lexical_score=row["lexical_score"],
            llm_score=row["llm_score"],
            relevance_score=row["relevance_score"],
            selection_reason=row["selection_reason"],
            selected=bool(row["selected"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class TraceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def append(
        self,
        project_id: str,
        trace_id: str,
        event_type: str,
        *,
        success: bool,
        agent: str | None = None,
        node: str | None = None,
        tool: str | None = None,
        latency_ms: int | None = None,
        summary: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> TraceRecord:
        identifier = str(uuid4())
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO traces(
                    id, trace_id, project_id, event_type, agent, node, tool,
                    success, latency_ms, summary_json, error_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    trace_id,
                    project_id,
                    event_type,
                    agent,
                    node,
                    tool,
                    int(success),
                    latency_ms,
                    dump_json(summary) if summary else None,
                    dump_json(error) if error else None,
                    now,
                ),
            )
            await connection.commit()
        records = await self.list_for_project(project_id)
        return next(record for record in records if record.id == identifier)

    async def list_for_project(
        self,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
        success: bool | None = None,
    ) -> list[TraceRecord]:
        conditions = ["project_id=?"]
        parameters: list[Any] = [project_id]
        if event_type is not None:
            conditions.append("event_type=?")
            parameters.append(event_type)
        if success is not None:
            conditions.append("success=?")
            parameters.append(int(success))
        parameters.extend([limit, offset])
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM traces WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at, id LIMIT ? OFFSET ?",
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._to_record(row) for row in rows]

    async def metrics(self, project_id: str) -> TraceMetrics:
        async with self.database.connect() as connection:
            totals = await (
                await connection.execute(
                    """
                    SELECT COUNT(*) total,
                           SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) successful,
                           AVG(latency_ms) average_latency,
                           SUM(CASE WHEN event_type='item_recovered' THEN 1 ELSE 0 END) recoveries
                    FROM traces WHERE project_id=?
                    """,
                    (project_id,),
                )
            ).fetchone()
            event_rows = await (
                await connection.execute(
                    "SELECT event_type, COUNT(*) count FROM traces WHERE project_id=? GROUP BY event_type",
                    (project_id,),
                )
            ).fetchall()
        total = totals["total"] or 0
        successful = totals["successful"] or 0
        return TraceMetrics(
            project_id=project_id,
            total_events=total,
            successful_events=successful,
            failed_events=total - successful,
            success_rate=successful / total if total else 0,
            average_latency_ms=totals["average_latency"],
            recovery_count=totals["recoveries"] or 0,
            by_event_type={row["event_type"]: row["count"] for row in event_rows},
        )

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> TraceRecord:
        return TraceRecord(
            id=row["id"],
            trace_id=row["trace_id"],
            project_id=row["project_id"],
            event_type=row["event_type"],
            agent=row["agent"],
            node=row["node"],
            tool=row["tool"],
            success=bool(row["success"]),
            latency_ms=row["latency_ms"],
            summary=load_json(row["summary_json"]),
            error=load_json(row["error_json"]),
            created_at=row["created_at"],
        )
