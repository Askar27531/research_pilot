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

    async def start(self) -> None:
        recovered = await self._jobs().recover_interrupted()
        await self._jobs().reconcile_failed_projects()
        self._task = asyncio.create_task(self._run(), name="researchpilot-workflow-worker")
        if recovered:
            self.wake()

    def _jobs(self) -> WorkflowJobRepository:
        return WorkflowJobRepository(self.app.state.database)

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

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

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self._jobs().claim_next()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2)
                except TimeoutError:
                    pass
                continue
            self._active_job = job
            try:
                self._active_execution = asyncio.create_task(self.workflow.execute_job(job))
                await self._active_execution
                await self._jobs().succeed(job.job_id)
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
            except Exception as exc:  # noqa: BLE001 - durable worker records its boundary
                error = {
                    "type": type(exc).__name__, "message": str(exc)[:1_000]
                }
                await self._jobs().fail(job.job_id, error)
                with suppress(RecordNotFoundError):
                    projects = ProjectRepository(self.app.state.database)
                    project = await projects.get(job.project_id)
                    await projects.fail(job.project_id, project.current_stage, error)
            finally:
                self._active_execution = None
                self._active_job = None
