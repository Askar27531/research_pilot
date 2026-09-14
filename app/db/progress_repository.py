from uuid import uuid4

from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import dump_json, load_json, utc_now
from app.schemas import ProgressMetrics, WorkItemProgress


class WorkItemRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    # Durable-Execution: 幂等单元核心：BEGIN IMMEDIATE 下原子“查-判-写”——completed 直接返回缓存(claimed=False，调用方零 token)；running/failed 置回 running 且 attempts+1 重跑；input_hash 不同默认抛冲突，replace_changed=True 时允许重置重跑（改要求后只重做该单元）。
    async def claim(
        self,
        project_id: str,
        run_scope: str,
        item_key: str,
        item_type: str,
        input_hash: str,
        *,
        replace_changed: bool = False,
    ) -> tuple[WorkItemProgress, bool]:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    "SELECT * FROM work_items WHERE project_id=? AND run_scope=? AND item_key=?",
                    (project_id, run_scope, item_key),
                )
            ).fetchone()
            if row is not None:
                if row["input_hash"] != input_hash:
                    if replace_changed:
                        await connection.execute(
                            """
                            UPDATE work_items SET status='running', attempts=attempts+1,
                                input_hash=?, result_json=NULL, error_json=NULL,
                                latency_ms=NULL, updated_at=? WHERE id=?
                            """,
                            (input_hash, now, row["id"]),
                        )
                        await connection.commit()
                        return await self.get(project_id, run_scope, item_key), True
                    await connection.rollback()
                    raise ProjectConflictError("Work item key was reused with different input")
                if row["status"] == "completed":
                    await connection.rollback()
                    return self._record(row), False
                await connection.execute(
                    """
                    UPDATE work_items SET status='running', attempts=attempts+1,
                        error_json=NULL, updated_at=? WHERE id=?
                    """,
                    (now, row["id"]),
                )
                await connection.commit()
                return await self.get(project_id, run_scope, item_key), True
            identifier = str(uuid4())
            await connection.execute(
                """
                INSERT INTO work_items(
                    id, project_id, run_scope, item_key, item_type, status,
                    attempts, input_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', 1, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    run_scope,
                    item_key,
                    item_type,
                    input_hash,
                    now,
                    now,
                ),
            )
            await connection.commit()
        return await self.get(project_id, run_scope, item_key), True

    # Durable-Execution: 成功落库 result_json——本行即该单元的“已付费章”，此后任何重试/续跑直接复用缓存，模型不再被调。
    async def complete(
        self,
        project_id: str,
        run_scope: str,
        item_key: str,
        result: dict,
        latency_ms: int,
    ) -> WorkItemProgress:
        return await self._finish(
            project_id, run_scope, item_key, "completed", result, None, latency_ms
        )

    # Durable-Execution: 失败落库错误快照；下次 claim 会 attempts+1 重新执行（失败重试本身，不是重复消耗）。
    async def fail(
        self,
        project_id: str,
        run_scope: str,
        item_key: str,
        error: dict,
        latency_ms: int,
    ) -> WorkItemProgress:
        return await self._finish(
            project_id, run_scope, item_key, "failed", None, error, latency_ms
        )

    async def _finish(
        self,
        project_id: str,
        run_scope: str,
        item_key: str,
        status: str,
        result: dict | None,
        error: dict | None,
        latency_ms: int,
    ) -> WorkItemProgress:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE work_items SET status=?, result_json=?, error_json=?,
                    latency_ms=?, updated_at=?
                WHERE project_id=? AND run_scope=? AND item_key=?
                """,
                (
                    status,
                    dump_json(result) if result is not None else None,
                    dump_json(error) if error is not None else None,
                    latency_ms,
                    utc_now(),
                    project_id,
                    run_scope,
                    item_key,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Work item not found: {item_key}")
            await connection.commit()
        return await self.get(project_id, run_scope, item_key)

    async def get(self, project_id: str, run_scope: str, item_key: str) -> WorkItemProgress:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM work_items WHERE project_id=? AND run_scope=? AND item_key=?",
                    (project_id, run_scope, item_key),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Work item not found: {item_key}")
        return self._record(row)

    async def list_for_project(self, project_id: str) -> list[WorkItemProgress]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT * FROM work_items WHERE project_id=? ORDER BY created_at, id",
                    (project_id,),
                )
            ).fetchall()
        return [self._record(row) for row in rows]

    # Durable-Execution: 局部重析：只删除目标 (paper, part) 的缓存行，其余 part 行原样复用——“只重跑被改的那一个部分”，最多付一次 LLM 调用。
    async def delete_part_items(
        self, project_id: str, revision: int, paper_id: str, item_keys: list[str]
    ) -> int:
        """Force selected (paper, part) cache entries to re-run on the next pass.

        Partial re-analysis deletes only these rows; every other part's cached
        result is reused, so a targeted rerun costs one LLM call at most.
        """
        if not item_keys:
            return 0
        markers = ",".join("?" for _ in item_keys)
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                f"DELETE FROM work_items WHERE project_id=? AND item_type="
                f"'paper_analysis_section' AND item_key IN ({markers}) "
                f"AND run_scope LIKE ?",
                (project_id, *item_keys, f"paper-analysis:{revision}:{paper_id}:%"),
            )
            await connection.commit()
        return cursor.rowcount

    # Durable-Execution: 增量视图：取某论文当前 revision 各 part 的 completed 缓存，供运行中 UI 实时渲染（论文还没分析完就能看到已完成部分），并按 updated_at 保证多指纹下不串 run。
    async def analysis_sections(
        self, project_id: str, revision: int, paper_id: str
    ) -> dict[str, dict]:
        """Completed per-part analysis results for one paper's current revision.

        Returns ``{item_key: {...draft...}}`` for every finished
        ``paper_analysis_section`` item whose value belongs to the latest
        ``paper-analysis:<revision>:<paper_id>:*`` run. When several
        fingerprints exist we keep the most recently updated row per part so the
        incremental view never mixes two runs. Used to render analysis content
        *while* the run is still in progress.
        """
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT item_key,status,result_json,updated_at FROM work_items "
                "WHERE project_id=? AND item_type='paper_analysis_section' "
                "AND run_scope LIKE ?",
                (project_id, f"paper-analysis:{revision}:{paper_id}:%"),
            )).fetchall()
        latest: dict[str, dict] = {}
        for row in rows:
            key = row["item_key"]
            if key not in latest or row["updated_at"] > latest[key]["updated_at"]:
                latest[key] = dict(row)
        result: dict[str, dict] = {}
        for key, row in latest.items():
            if row["status"] == "completed" and row["result_json"]:
                result[key] = load_json(row["result_json"])
        return result

    async def metrics(self, project_id: str) -> ProgressMetrics:
        items = await self.list_for_project(project_id)
        return ProgressMetrics(
            project_id=project_id,
            total_items=len(items),
            completed_items=sum(item.status == "completed" for item in items),
            failed_items=sum(item.status == "failed" for item in items),
            running_items=sum(item.status == "running" for item in items),
            recovered_items=sum(item.status == "completed" and item.attempts > 1 for item in items),
        )

    @staticmethod
    def _record(row) -> WorkItemProgress:
        return WorkItemProgress(
            item_id=row["id"],
            project_id=row["project_id"],
            run_scope=row["run_scope"],
            item_key=row["item_key"],
            item_type=row["item_type"],
            status=row["status"],
            attempts=row["attempts"],
            input_hash=row["input_hash"],
            result=load_json(row["result_json"]),
            error=load_json(row["error_json"]),
            latency_ms=row["latency_ms"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
