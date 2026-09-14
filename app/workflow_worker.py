import asyncio
from contextlib import suppress

from app.db import ProjectRepository, WorkflowJobRepository
from app.db.errors import RecordNotFoundError
from app.literature import LiteratureToolClient
from app.services import ResearchWorkflowService


class WorkflowWorker:
    """Single-machine durable queue runner."""

    def __init__(self, app, literature: LiteratureToolClient) -> None:
        self.app = app
        self.workflow = ResearchWorkflowService(app, literature)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._active_job = None
        self._active_execution: asyncio.Task | None = None

    # Durable-Execution: worker 启动入口：recover_interrupted() 把遗留 running 任务放回队列并置项目 waiting，reconcile_failed_projects() 兜底僵尸项目；随后启动领取循环（有恢复任务立即唤醒）。
    async def start(self) -> None:                         # worker 启动入口（lifespan 里调用一次）
        recovered = await self._jobs().recover_interrupted()   # 启动收尸：把遗留 running 任务放回 queued
        await self._jobs().reconcile_failed_projects()      # 兜底自愈：修正"项目 running 但任务已 failed"的僵尸
        self._task = asyncio.create_task(self._run(), name="researchpilot-workflow-worker")  # ← 点火：把主循环登记成常驻后台 Task
        if recovered:
            self.wake()                                     # 有恢复任务 → 立刻叫醒去领

    def _jobs(self) -> WorkflowJobRepository:
        return WorkflowJobRepository(self.app.state.database)

    def wake(self) -> None:                                # 同步方法：只置门铃标志
        self._wake.set()                                   # ← 把 _wake Event 置位，唤醒睡在 _run() 里的协程

    # Durable-Execution: 优雅停机：置停止位并取消主循环；配合 recover_interrupted 保证重启时任务状态一致性。
    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    # Durable-Execution: 删除项目前先取消进程内正在跑的该任务执行再置任务终态，防止删除与运行中写并发。
    async def cancel_project(self, project_id: str) -> None:
        """Stop an in-process job so project deletion cannot race with its writes."""
        execution = self._active_execution
        if (
            self._active_job is not None
            and self._active_job.project_id == project_id
            and execution is not None
            and not execution.done()
        ):
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
        await self._jobs().cancel_for_project(project_id)

    # Durable-Execution: 单机持久化队列主循环：原子认领一条任务 → execute_job() → succeed()/fail() 双写终态；任何异常的收口边界都在这里，是“任务层断点续跑”的执行器。
    async def _run(self) -> None:
        while not self._stop.is_set():                    # 后台常驻循环：没被叫停就一直跑
            job = await self._jobs().claim_next()         # ← 去数据库领一条"排队中"的任务
            if job is None:                                # 没活干
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2)  # ← 睡这：等 wake() 叫醒，或 2 秒心跳兜底
                except TimeoutError:
                    pass
                continue                                   # 醒来回循环顶部再查一次
            self._active_job = job                         # 记下当前任务（供 cancel_project 取消用）
            try:
                self._active_execution = asyncio.create_task(self.workflow.execute_job(job))  # ← 点火：登记成后台 Task，留句柄
                await self._active_execution               # ← 挂起等任务跑完（单 worker 一次只跑一件）
                await self._jobs().succeed(job.job_id)     # 跑完没炸 → 任务标"成功"
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
            except Exception as exc:  # noqa: BLE001 - durable worker records its boundary
                error = {
                    "type": type(exc).__name__, "message": str(exc)[:1_000]
                }
                await self._jobs().fail(job.job_id, error)  # 跑炸了 → 任务标"失败" + 错误快照
                with suppress(RecordNotFoundError):
                    projects = ProjectRepository(self.app.state.database)
                    project = await projects.get(job.project_id)
                    await projects.fail(job.project_id, project.current_stage, error)  # 项目也标失败（双写）
            finally:
                self._active_execution = None               # 清句柄
                self._active_job = None
