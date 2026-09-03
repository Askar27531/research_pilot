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
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            goal=row["goal"],
            request=ResearchRequest.model_validate_json(row["request_json"]),
            status=row["status"],
            current_stage=row["current_stage"],
            last_run_id=row["last_run_id"],
            error=load_json(row["error_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
        )


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
