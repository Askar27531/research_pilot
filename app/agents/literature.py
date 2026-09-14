import hashlib
import json
from collections.abc import Sequence
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from app.db import TraceRepository, WorkItemRepository
from app.graph import build_literature_search_graph, create_research_state
from app.literature import LiteratureToolClient
from app.llm import LLMProvider
from app.reliability import FailureInjector
from app.schemas import (
    AgentResult,
    AgentTask,
    ResearchRequest,
    SearchResult,
)
from app.skills import SkillRegistry
from app.skills.bindings import SKILL_FOR_RESPONSE_MODEL


class TracedLiteratureClient:
    def __init__(
        self,
        client: LiteratureToolClient,
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

    # Durable-Execution: 检索调用的幂等封装：把查询参数做 SHA-256 指纹去 claim；命中 completed 直接用缓存返回（外部源不再被打），attempts>1 时记 item_recovered 审计事件。
    async def search_papers(
        self,
        query: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 20,
        sources: list[str] | None = None,
    ) -> Any:
        payload = {
            "query": query,
            "year_from": year_from,
            "year_to": year_to,
            "limit": limit,
            "sources": sources,
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
            result = await self.client.search_papers(
                query, year_from, year_to, limit, sources=sources
            )
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
        skill_name = SKILL_FOR_RESPONSE_MODEL.get(response_model)
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
        literature: LiteratureToolClient,
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

    # Durable-Execution: 检索图运行入口：thread_id = 项目:search-{revision}，把该次检索的“档位”钉在 SQLite checkpoint；resume 时以 ainvoke(None) 同线程续跑，否则全量输入开新 run。
    async def run(self, task: AgentTask, trace_id: str) -> AgentResult:
        request = ResearchRequest.model_validate(task.context["request"])   # 课题 JSON → 对象
        skill_provider = SkillAwareProvider(        # 给 LLM 套一层"技能注入"（按 response_model 塞 system 消息）
            self.provider, self.skills, self.traces, task.project_id, trace_id
        )
        traced_client = TracedLiteratureClient(     # 给文献检索套一层"幂等缓存 + 追踪"（按参数指纹不重复打外部 API）
            self.literature, self.traces, task.project_id, trace_id, self.items
        )
        graph = build_literature_search_graph(      # 组装 8 节点检索图（带 checkpointer）
            skill_provider, traced_client, self.checkpointer
        )
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": f"{task.project_id}:search-{task.context.get('search_revision', 1)}"   # ← 档位标识（续跑从这读快照）
            },
            "metadata": {"trace_id": trace_id, "project_id": task.project_id},
        }
        if task.context.get("resume"):              # 续跑：不传初始状态，从 checkpoint 读
            state = await graph.ainvoke(None, config=config)
        else:
            state = await graph.ainvoke(            # ← 从头跑：注入全新初始状态，驱动 8 节点
                create_research_state(request, project_id=task.project_id), config=config
            )
        return AgentResult(
            task_id=task.task_id,
            project_id=task.project_id,
            agent=self.name,                        # "literature_researcher"
            status="completed",
            summary=f"Selected {len(state['selected_papers'])} papers",
            output=state,                           # ← 完整图状态（ranked_papers/understanding/queries）
            loaded_skills=skill_provider.loaded_skills,
        )
