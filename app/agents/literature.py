import hashlib
import json
from collections.abc import Sequence
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from app.db import TraceRepository, WorkItemRepository
from app.graph import build_literature_search_graph, create_research_state
from app.literature import LiteratureMCPClient
from app.llm import LLMProvider
from app.reliability import FailureInjector
from app.schemas import (
    AgentResult,
    AgentTask,
    Handoff,
    PaperScreeningBatch,
    ResearchRequest,
    SearchQueryPlan,
    SearchResult,
)
from app.skills import SkillRegistry


class TracedLiteratureClient:
    def __init__(
        self,
        client: LiteratureMCPClient,
        traces: TraceRepository,
        project_id: str,
        trace_id: str,
        items: WorkItemRepository,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.client = client
        self.traces = traces
        self.project_id = project_id
        self.trace_id = trace_id
        self.items = items
        self.failure_injector = failure_injector

    async def search_papers(
        self,
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
    ) -> Any:
        payload = {
            "query": query,
            "year_from": year_from,
            "year_to": year_to,
            "limit": limit,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        input_hash = hashlib.sha256(encoded).hexdigest()
        item_key = f"query:{input_hash}"
        progress, claimed = await self.items.claim(
            self.project_id, "literature_search", item_key, "literature_query", input_hash
        )
        if not claimed:
            await self.traces.append(
                self.project_id,
                self.trace_id,
                "item_cache_hit",
                success=True,
                agent="literature_researcher",
                tool="Literature.search_papers",
                summary={"item_key": item_key, "attempts": progress.attempts},
            )
            return SearchResult.model_validate(progress.result)
        started = perf_counter()
        try:
            if self.failure_injector is not None:
                self.failure_injector.before_item()
            if isinstance(self.client, LiteratureMCPClient):
                result = await self.client.search_papers(
                    query, year_from, year_to, limit, trace_id=self.trace_id
                )
            else:
                result = await self.client.search_papers(query, year_from, year_to, limit)
        except Exception as exc:
            latency = round((perf_counter() - started) * 1000)
            await self.items.fail(
                self.project_id,
                "literature_search",
                item_key,
                {"type": type(exc).__name__, "message": str(exc)[:1_000]},
                latency,
            )
            await self.traces.append(
                self.project_id,
                self.trace_id,
                "tool_call",
                success=False,
                agent="literature_researcher",
                tool="Literature.search_papers",
                summary={"query": query[:500]},
                error={"type": type(exc).__name__, "message": str(exc)[:1_000]},
            )
            raise
        latency = round((perf_counter() - started) * 1000)
        completed = await self.items.complete(
            self.project_id,
            "literature_search",
            item_key,
            result.model_dump(mode="json"),
            latency,
        )
        if completed.attempts > 1:
            await self.traces.append(
                self.project_id,
                self.trace_id,
                "item_recovered",
                success=True,
                agent="literature_researcher",
                tool="Literature.search_papers",
                latency_ms=latency,
                summary={"item_key": item_key, "attempts": completed.attempts},
            )
        await self.traces.append(
            self.project_id,
            self.trace_id,
            "tool_call",
            success=True,
            agent="literature_researcher",
            tool="Literature.search_papers",
            summary={"query": query[:500], "paper_count": len(result.papers)},
        )
        return result


class SkillAwareProvider(LLMProvider):
    def __init__(
        self,
        provider: LLMProvider,
        skills: SkillRegistry,
        traces: TraceRepository,
        project_id: str,
        trace_id: str,
    ) -> None:
        self.provider = provider
        self.skills = skills
        self.traces = traces
        self.project_id = project_id
        self.trace_id = trace_id
        self.loaded_skills: list[str] = []

    async def chat(self, messages: Sequence[dict[str, str]]) -> str:
        return await self.provider.chat(messages)

    async def structured_output(
        self,
        messages: Sequence[dict[str, str]],
        response_model: type[BaseModel],
    ) -> BaseModel:
        mapping = {
            SearchQueryPlan: "systematic-search",
            PaperScreeningBatch: "paper-screening",
        }
        skill_name = mapping.get(response_model)
        effective_messages = list(messages)
        if skill_name is not None:
            skill = await self._load(skill_name)
            effective_messages.insert(
                0,
                {
                    "role": "system",
                    "content": f"Apply this workflow skill:\n\n{skill.content}",
                },
            )
        return await self.provider.structured_output(effective_messages, response_model)

    async def close(self) -> None:
        return None

    async def _load(self, name: str):
        skill = self.skills.load_skill(name)
        if name not in self.loaded_skills:
            self.loaded_skills.append(name)
            await self.traces.append(
                self.project_id,
                self.trace_id,
                "skill_load",
                success=True,
                agent="literature_researcher",
                summary={"name": skill.name, "version": skill.version},
            )
        return skill


class LiteratureResearcher:
    name = "literature_researcher"

    def __init__(
        self,
        provider: LLMProvider,
        literature: LiteratureMCPClient,
        checkpointer: Any,
        skills: SkillRegistry,
        traces: TraceRepository,
        items: WorkItemRepository,
    ) -> None:
        self.provider = provider
        self.literature = literature
        self.checkpointer = checkpointer
        self.skills = skills
        self.traces = traces
        self.items = items

    async def run(self, task: AgentTask, handoff: Handoff, trace_id: str) -> AgentResult:
        request = ResearchRequest.model_validate(task.context["request"])
        skill_provider = SkillAwareProvider(
            self.provider, self.skills, self.traces, task.project_id, trace_id
        )
        traced_client = TracedLiteratureClient(
            self.literature, self.traces, task.project_id, trace_id, self.items
        )
        graph = build_literature_search_graph(skill_provider, traced_client, self.checkpointer)
        config: dict[str, Any] = {
            "configurable": {"thread_id": task.project_id},
            "metadata": {"trace_id": trace_id, "project_id": task.project_id},
        }
        if task.context.get("resume"):
            state = await graph.ainvoke(None, config=config)
        else:
            state = await graph.ainvoke(
                create_research_state(request, project_id=task.project_id), config=config
            )
        return AgentResult(
            task_id=task.task_id,
            project_id=task.project_id,
            agent=self.name,
            status="completed",
            summary=f"Selected {len(state['selected_papers'])} papers",
            output=state,
            loaded_skills=skill_provider.loaded_skills,
        )
