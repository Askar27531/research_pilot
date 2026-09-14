from uuid import uuid4

from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import dump_json, load_json, utc_now
from app.schemas import WorkflowJob


class WorkflowJobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    # Durable-Execution: 幂等入队：同一项目已有 queued/running 任务时直接返回现有记录，不重复排队；配合“每项目一活动任务”的部分唯一索引在数据库层禁止双任务。
    async def enqueue(self, project_id: str, job_type: str = "research") -> WorkflowJob:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            project = await (await connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            )).fetchone()
            if project is None:
                await connection.rollback()
                raise RecordNotFoundError(f"Project not found: {project_id}")
            row = await (await connection.execute(
                "SELECT * FROM workflow_jobs WHERE project_id=? "
                "AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
                (project_id,),
            )).fetchone()
            if row is not None:
                await connection.rollback()
                return self._to_job(row)
            identifier = str(uuid4())
            run_id = f"workspace:{identifier}"
            await connection.execute(
                "INSERT INTO workflow_jobs(id,project_id,run_id,status,attempts,created_at,updated_at,job_type) "
                "VALUES(?,?,?,'queued',0,?,?,?)",
                (identifier, project_id, run_id, now, now, job_type),
            )
            await connection.commit()
        return await self.get(identifier)

    async def get(self, job_id: str) -> WorkflowJob:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM workflow_jobs WHERE id=?", (job_id,)
            )).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Workflow job not found: {job_id}")
        return self._to_job(row)

    async def latest_for_project(self, project_id: str) -> WorkflowJob | None:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM workflow_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )).fetchone()
        return self._to_job(row) if row is not None else None

    async def latest_for_projects(self, project_ids: list[str]) -> dict[str, WorkflowJob]:
        if not project_ids:
            return {}
        placeholders = ",".join("?" for _ in project_ids)
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                f"SELECT * FROM workflow_jobs WHERE project_id IN ({placeholders}) "
                "ORDER BY created_at DESC,id DESC",
                project_ids,
            )).fetchall()
        result: dict[str, WorkflowJob] = {}
        for row in rows:
            result.setdefault(row["project_id"], self._to_job(row))
        return result

    # Durable-Execution: 原子认领下一条任务：BEGIN IMMEDIATE 下把最老 queued 置 running 并 attempts+1（单写者模型下不会双领）；attempts 记录该任务被领取/重试的次数。
    async def claim_next(self) -> WorkflowJob | None:      # worker 从这里领任务
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")    # 抢写锁，保证原子领取
            row = await (await connection.execute(
                "SELECT * FROM workflow_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"  # 取"最老"的一条排队任务
            )).fetchone()
            if row is None:                                # 没活
                await connection.rollback()
                return None
            await connection.execute(
                "UPDATE workflow_jobs SET status='running',attempts=attempts+1,updated_at=? "
                "WHERE id=? AND status='queued'", (now, row["id"]),   # ← queued→running，防重复领取
            )
            await connection.commit()
        return await self.get(row["id"])                   # 读回完整 job 返回

    # Durable-Execution: 任务成功终态落库；job 状态（queued/running/succeeded/failed）是重启后是否重领的判据。
    async def succeed(self, job_id: str) -> WorkflowJob:   # 成功终态
        return await self._finish(job_id, "succeeded", None)

    # Durable-Execution: 任务失败终态落库并带错误快照；失败任务可被 resume/重试再次领取。
    async def fail(self, job_id: str, error: dict) -> WorkflowJob:   # 失败终态（带错误快照）
        return await self._finish(job_id, "failed", error)

    # Durable-Execution: 删除项目前把该项目的 queued/running 任务统一置为 failed(Cancelled)，终止一切在途调度。
    async def cancel_for_project(self, project_id: str) -> int:
        """Make queued/running jobs terminal before a project is deleted."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE workflow_jobs SET status='failed',error_json=?,updated_at=? "
                "WHERE project_id=? AND status IN ('queued','running')",
                (dump_json({"type": "Cancelled", "message": "Project deleted"}),
                 utc_now(), project_id),
            )
            await connection.commit()
        return cursor.rowcount

    async def _finish(self, job_id: str, status: str, error: dict | None) -> WorkflowJob:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "UPDATE workflow_jobs SET status=?,error_json=?,updated_at=? WHERE id=?",
                (status, dump_json(error) if error else None, utc_now(), job_id),   # 任务落终态（succeeded/failed）
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Workflow job not found: {job_id}")
            await connection.commit()
        return await self.get(job_id)

    # Durable-Execution: 启动收尸：running 状态在进程死亡后是假象 → 全部放回 queued（同一 job/run_id，分析图 thread 不变，重入即“按原 run 续跑”）；对应项目置 waiting + 'recovering'。
    async def recover_interrupted(self) -> int:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            rows = await (await connection.execute(
                "SELECT DISTINCT project_id FROM workflow_jobs WHERE status='running'"
            )).fetchall()
            await connection.execute(
                "UPDATE workflow_jobs SET status='queued',updated_at=? WHERE status='running'", (now,)
            )
            for row in rows:
                await connection.execute(
                    "UPDATE projects SET status='waiting',current_stage='recovering',updated_at=?,"
                    "version=version+1 WHERE id=? AND status='running'", (now, row["project_id"]),
                )
            await connection.commit()
        return len(rows)

    # Durable-Execution: 兜底自愈：项目仍停在 running 但其最新 job 已 failed（上次失败时双写未完成）的僵尸，启动时统一修正为 failed。
    async def reconcile_failed_projects(self) -> int:
        """Repair projects left running after their latest job failed."""
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            rows = await (await connection.execute(
                "SELECT p.id,j.error_json FROM projects p JOIN workflow_jobs j ON j.id=("
                "SELECT latest.id FROM workflow_jobs latest WHERE latest.project_id=p.id "
                "ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1) "
                "WHERE p.status='running' AND j.status='failed'"
            )).fetchall()
            for row in rows:
                await connection.execute(
                    "UPDATE projects SET status='failed',error_json=?,updated_at=?,"
                    "version=version+1 WHERE id=?",
                    (row["error_json"], now, row["id"]),
                )
            await connection.commit()
        return len(rows)

    @staticmethod
    def _to_job(row) -> WorkflowJob:
        return WorkflowJob(job_id=row["id"], project_id=row["project_id"], run_id=row["run_id"],
            status=row["status"], job_type=dict(row).get("job_type", "research"),
            attempts=row["attempts"], error=load_json(row["error_json"]),
            created_at=row["created_at"], updated_at=row["updated_at"])
