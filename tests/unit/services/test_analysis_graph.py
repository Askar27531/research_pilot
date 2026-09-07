"""Graph ② (analysis_graph) M1/M2 regression: topology compiles; the SelectGate
parks + interrupts through a real graph run (MemorySaver) and proceeds on a
selection-backed resume."""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph.analysis_graph import build_analysis_graph
from app.graph.analysis_nodes import (
    AnalysisGraphContext,
    AnalysisJobStop,
    make_review_gate_node,
    make_select_gate_node,
)
from app.graph.analysis_state import AnalysisState
from app.schemas import AnalysisClaim, PaperAnalysis


class _Project:
    def __init__(self, goal: str) -> None:
        self.goal = goal


class _ProjectsStub:
    def __init__(self) -> None:
        self.parked: list[dict] = []

    async def get(self, project_id: str) -> _Project:
        return _Project("研究目标")

    async def wait_for_human(self, project_id: str, stage: str, **kwargs) -> _Project:
        self.parked.append({"project_id": project_id, "stage": stage, **kwargs})
        return _Project("研究目标")


class _SessionsStub:
    def __init__(self, selection: dict | None = None) -> None:
        self.selection_value = selection

    async def selection(self, project_id: str) -> dict | None:
        return self.selection_value

    async def current_search(self, project_id: str) -> dict:
        return {"revision": 1, "status": "completed"}

    async def current_paper_ids(self, project_id: str, revision: int) -> list[str]:
        return ["p-1", "p-2"]


def _ctx() -> tuple[AnalysisGraphContext, _ProjectsStub, _SessionsStub]:
    projects = _ProjectsStub()
    sessions = _SessionsStub()
    return AnalysisGraphContext(
        project_id="project-1",
        run_id="run-1",
        sessions=sessions,
        projects=projects,
    ), projects, sessions


def _gate_graph(ctx: AnalysisGraphContext):
    builder = StateGraph(AnalysisState)
    builder.add_node("select_gate", make_select_gate_node(ctx))
    builder.add_edge(START, "select_gate")
    builder.add_edge("select_gate", END)
    return builder.compile(checkpointer=MemorySaver())


def _thread(project_id: str) -> dict:
    return {"configurable": {"thread_id": f"{project_id}:analysis:test-1"}}


def test_analysis_graph_compiles_with_gate_topology() -> None:
    graph = build_analysis_graph(_ctx()[0])
    nodes = {node for node in graph.get_graph().nodes}
    assert {"select_gate", "acquire_documents", "analyze_papers", "review_gate",
            "synthesize"} <= nodes


@pytest.mark.asyncio
async def test_select_gate_proceeds_when_selection_exists() -> None:
    ctx, projects, sessions = _ctx()
    sessions.selection_value = {
        "search_revision": 3,
        "paper_ids": ["p-1", "p-2"],
        "requirements": "只报告方法",
    }
    node = make_select_gate_node(ctx)

    state = await node({})

    assert state["search_revision"] == 3
    assert state["paper_ids"] == ["p-1", "p-2"]
    assert state["requirements"] == "只报告方法"
    assert state["goal"] == "研究目标"
    assert projects.parked == []  # gate only parks when the selection is missing


@pytest.mark.asyncio
async def test_select_gate_interrupts_via_real_graph_and_resumes() -> None:
    ctx, projects, sessions = _ctx()
    graph = _gate_graph(ctx)
    thread = _thread(ctx.project_id)

    # First pass: no selection → gate parks (event) then freezes the graph;
    # LangGraph surfaces the freeze as an ``__interrupt__`` channel on the
    # returned state (no exception raised out of ainvoke).
    state = await graph.ainvoke({}, config=thread)
    assert state.get("__interrupt__")
    assert len(projects.parked) == 1
    assert projects.parked[0]["stage"] == "paper_selection"
    assert projects.parked[0]["event_type"] == "paper_selection"
    assert projects.parked[0]["run_id"] == "run-1"

    # Human picked papers (action saved the selection) → resume the same thread:
    # the gate re-reads the selection and proceeds past the node.
    sessions.selection_value = {
        "search_revision": 3,
        "paper_ids": ["p-1"],
        "requirements": None,
    }
    state = await graph.ainvoke(
        Command(resume={"action": "select_papers"}), config=thread
    )
    assert state["search_revision"] == 3
    assert state["paper_ids"] == ["p-1"]
    assert state["goal"] == "研究目标"
    # The wait was resolved by the action before resuming: no second park.
    assert len(projects.parked) == 1


def test_analysis_job_stop_is_graceful_stop_sentinel() -> None:
    error = AnalysisJobStop("parked")
    assert isinstance(error, RuntimeError)
    assert str(error) == "parked"


# ---------------------------------------------------------------------------
# M3 ReviewGate
# ---------------------------------------------------------------------------

def _paper(paper_id: str, evidence_ids: list[str] | None = None) -> PaperAnalysis:
    """A paper whose first claim cites *evidence_ids* (supported) or no
    evidence (inference — supported claims require evidence by schema)."""
    if evidence_ids:
        claim = AnalysisClaim(value="结论", kind="supported", evidence_ids=evidence_ids)
    else:
        claim = AnalysisClaim(value="推断", kind="inference")
    return PaperAnalysis(
        paper_id=paper_id, title=f"Paper {paper_id}",
        core_problem=[claim],
        methods=[], mechanisms=[], experimental_setup=[], main_results=[],
        limitations=[], relevance_to_topic=[], overview=None,
    )


class _ReviewProject:
    def __init__(self) -> None:
        self.goal = "研究目标"
        self.analysis_review_decision: str | None = None
        self.pause_reason: str | None = None

    def __getattr__(self, name: str):
        raise AttributeError(name)


class _ReviewProjectsStub:
    def __init__(self, decision: str | None = None) -> None:
        self.project = _ReviewProject()
        self.project.analysis_review_decision = decision
        self.parked: list[dict] = []
        self.decisions: list[str | None] = []

    async def get(self, project_id: str) -> _ReviewProject:
        return self.project

    async def wait_for_human(self, project_id: str, stage: str, **kwargs) -> _ReviewProject:
        self.parked.append({"stage": stage, **kwargs})
        return self.project

    async def set_pause_reason(self, project_id: str, reason: str | None) -> _ReviewProject:
        self.project.pause_reason = reason
        return self.project

    async def set_analysis_review_decision(self, project_id: str, decision: str | None):
        self.project.analysis_review_decision = decision
        self.decisions.append(decision)
        return self.project


class _ReviewSessionsStub:
    def __init__(self, analyses: list[PaperAnalysis]) -> None:
        self.analyses = analyses
        self.resets = 0

    async def paper_analyses(self, project_id: str, revision: int) -> list[PaperAnalysis]:
        return self.analyses

    async def reset_analysis(self, project_id: str, revision: int) -> None:
        self.resets += 1


class _ReviewResearchStub:
    def __init__(self, reviews: list[dict]) -> None:
        self.reviews = reviews

    async def list_reviews(self, project_id: str) -> list[dict]:
        return self.reviews


def _review_ctx(doubted_cited: bool, decision: str | None = None):
    analyses = [_paper("p-1", ["e-1"] if doubted_cited else [])]
    reviews = (
        [{"evidence_id": "e-1", "status": "doubted", "source": "auto_consistency"}]
        if doubted_cited else []
    )
    projects = _ReviewProjectsStub(decision)
    return AnalysisGraphContext(
        project_id="project-1",
        run_id="run-1",
        projects=projects,
        sessions=_ReviewSessionsStub(analyses),
        research=_ReviewResearchStub(reviews),
    ), projects


def _review_gate_graph(ctx: AnalysisGraphContext):
    builder = StateGraph(AnalysisState)
    builder.add_node("review_gate", make_review_gate_node(ctx))
    builder.add_edge(START, "review_gate")
    builder.add_edge("review_gate", END)
    return builder.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_review_gate_passes_without_high_risk_evidence() -> None:
    ctx, projects = _review_ctx(doubted_cited=False)
    state = await make_review_gate_node(ctx)({"search_revision": 1})

    assert state["review_outcome"] == "continue"
    assert projects.parked == []


@pytest.mark.asyncio
async def test_review_gate_parks_and_interrupts_on_high_risk() -> None:
    ctx, projects = _review_ctx(doubted_cited=True)
    graph = _review_gate_graph(ctx)
    thread = {"configurable": {"thread_id": "project-1:analysis:review-1"}}

    state = await graph.ainvoke({"search_revision": 1}, config=thread)

    assert state.get("__interrupt__")
    assert projects.project.pause_reason == "review_gate"
    assert len(projects.parked) == 1
    assert projects.parked[0]["stage"] == "analysis_paused"
    assert projects.parked[0]["event_type"] == "evidence_review_gate"


@pytest.mark.asyncio
async def test_review_gate_continue_decision_skips_wait() -> None:
    ctx, projects = _review_ctx(doubted_cited=True, decision="continue")
    graph = _review_gate_graph(ctx)

    state = await graph.ainvoke({"search_revision": 1}, config={
        "configurable": {"thread_id": "project-1:analysis:review-2"}
    })

    assert state["review_outcome"] == "continue"
    assert projects.parked == []
    # The decision was consumed (cleared) so a later re-analysis re-evaluates.
    assert projects.project.analysis_review_decision is None


@pytest.mark.asyncio
async def test_review_gate_regenerate_clears_analyses_and_loops() -> None:
    ctx, projects = _review_ctx(doubted_cited=True, decision="regenerate_after_review")
    sessions = ctx.sessions  # type: ignore[attr-defined]
    graph = _review_gate_graph(ctx)

    state = await graph.ainvoke({"search_revision": 1}, config={
        "configurable": {"thread_id": "project-1:analysis:review-3"}
    })

    assert state["review_outcome"] == "regenerated"
    assert sessions.resets == 1
    assert projects.project.analysis_review_decision is None
    assert projects.parked == []
