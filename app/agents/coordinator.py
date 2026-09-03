from app.agents.literature import LiteratureResearcher
from app.db import TraceRepository
from app.schemas import AgentResult, AgentTask, Handoff


class Coordinator:
    """Route structured tasks; never execute specialist work itself."""

    def __init__(
        self,
        literature_researcher: LiteratureResearcher,
        traces: TraceRepository,
    ) -> None:
        self.literature_researcher = literature_researcher
        self.traces = traces

    async def run(self, task: AgentTask, trace_id: str) -> AgentResult:
        await self.traces.append(
            task.project_id,
            trace_id,
            "agent_plan",
            success=True,
            agent="coordinator",
            summary={"task_id": task.task_id, "task_type": task.task_type},
        )
        handoff = Handoff(
            task_id=task.task_id,
            project_id=task.project_id,
            source_agent="coordinator",
            target_agent="literature_researcher",
            objective=task.objective,
            context_summary={
                "has_request": "request" in task.context,
                "resume": bool(task.context.get("resume")),
            },
        )
        await self.traces.append(
            task.project_id,
            trace_id,
            "agent_handoff",
            success=True,
            agent="coordinator",
            summary=handoff.model_dump(mode="json"),
        )
        result = await self.literature_researcher.run(task, handoff, trace_id)
        await self.traces.append(
            task.project_id,
            trace_id,
            "agent_return",
            success=result.status != "failed",
            agent=result.agent,
            summary={"target": "coordinator", "status": result.status},
            error=result.error,
        )
        return result
