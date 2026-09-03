import hashlib
import json
from collections.abc import Sequence
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from app.db import TraceRepository, WorkItemRepository
from app.graph import build_literature_search_graph, create_research_state
from app.literature import LiteratureToolClient
from app.llm import LLMError, LLMProvider
from app.reliability import FailureInjector
from app.schemas import (
    AgentResult,
    AgentTask,
    Handoff,
    ResearchRequest,
    ResearchUnderstanding,
    SearchQuery,
    SearchQueryPlan,
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
            try:
                result = await self.client.search_papers(
                    query, year_from, year_to, limit, trace_id=self.trace_id, sources=sources
                )
            except TypeError:
                try:
                    result = await self.client.search_papers(
                        query, year_from, year_to, limit, sources=sources)
                except TypeError:
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

    async def run(self, task: AgentTask, handoff: Handoff, trace_id: str) -> AgentResult:
        request = ResearchRequest.model_validate(task.context["request"])
        skill_provider = SkillAwareProvider(
            self.provider, self.skills, self.traces, task.project_id, trace_id
        )
        traced_client = TracedLiteratureClient(
            self.literature, self.traces, task.project_id, trace_id, self.items
        )
        requests = [("domain", request)]
        profile = task.context.get("profile")
        if profile:
            challenges = profile.get("pain_points") or profile.get("objectives") or []
            donor_question = (
                "Find transferable methods that address these research challenges: "
                + "; ".join(challenges)
                + ". Prefer mechanism papers whose assumptions can be evaluated in the target topic."
            )
            requests.append(("method-donor", ResearchRequest(
                research_question=donor_question,
                keywords=list(dict.fromkeys([*request.keywords, *challenges]))[:30],
                year_from=request.year_from, year_to=request.year_to,
                maximum_papers=max(3, request.maximum_papers // 2),
                constraints=profile.get("constraints", []),
            )))
        states = []
        for track, track_request in requests:
            track_provider = skill_provider if track == "domain" else DeterministicDonorProvider()
            graph = build_literature_search_graph(track_provider, traced_client, self.checkpointer)
            config: dict[str, Any] = {
                "configurable": {"thread_id": (
                    f"{task.project_id}:search-{task.context.get('search_revision', 1)}:{track}"
                )},
                "metadata": {"trace_id": trace_id, "project_id": task.project_id, "track": track},
            }
            if task.context.get("resume"):
                state = await graph.ainvoke(None, config=config)
            else:
                state = await graph.ainvoke(
                    create_research_state(track_request, project_id=task.project_id), config=config
                )
            states.append(state)
        state = states[0]
        if len(states) > 1:
            merged = {}
            for item in [item for current in states for item in current["selected_papers"]]:
                stable_id = item["paper"]["stable_id"]
                if stable_id not in merged or item["final_score"] > merged[stable_id]["final_score"]:
                    merged[stable_id] = item
            state = dict(state)
            state["selected_papers"] = sorted(
                merged.values(), key=lambda item: item["final_score"], reverse=True
            )[: request.maximum_papers]
            state["current_stage"] = "papers_selected"
        return AgentResult(
            task_id=task.task_id,
            project_id=task.project_id,
            agent=self.name,
            status="completed",
            summary=f"Selected {len(state['selected_papers'])} papers",
            output=state,
            loaded_skills=skill_provider.loaded_skills,
        )


class DeterministicDonorProvider(LLMProvider):
    """Select existing deterministic fallbacks for the profile-derived donor track."""

    async def chat(self, messages: Sequence[dict[str, str]]) -> str:
        raise LLMError("Method-donor track uses deterministic profile-derived planning")

    async def structured_output(
        self, messages: Sequence[dict[str, str]], response_model: type[BaseModel]
    ) -> BaseModel:
        user_content = messages[-1]["content"]
        request = ResearchRequest.model_validate_json(user_content.split("\nUnderstanding:", 1)[0])
        if response_model is ResearchUnderstanding:
            return ResearchUnderstanding(
                normalized_goal=request.research_question,
                core_concepts=request.keywords or [request.research_question],
                domain="method donor search",
                year_from=request.year_from,
                year_to=request.year_to,
            )
        if response_model is SearchQueryPlan:
            concepts = request.keywords or [request.research_question]
            seeds = request.keywords[:3] or [request.research_question]
            while len(seeds) < 3:
                suffix = ("survey", "method algorithm")[len(seeds) - 1]
                seeds.append(f"{seeds[0]} {suffix}")
            purposes = ("high_precision", "synonym_expansion", "method_expansion")
            return SearchQueryPlan(
                topic=request.research_question,
                required_concept_groups=[],
                queries=[
                    SearchQuery(query=seed, purpose=purpose, concepts=concepts)
                    for seed, purpose in zip(seeds, purposes, strict=True)
                ],
            )
        raise LLMError("Method-donor screening uses deterministic lexical ranking")

    async def close(self) -> None:
        return None
