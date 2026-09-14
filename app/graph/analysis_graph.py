"""Graph ②: the durable paper-analysis pipeline (analysis_graph).

M3 topology (SelectGate + ReviewGate interrupts):

    START ─▶ select_gate ─▶ acquire_documents ─▶ analyze_papers ─▶ review_gate
           ─(regenerated)→ analyze_papers        │
           ─(continue)──────────────▶ synthesize ─▶ END

- ``select_gate`` (M2, interrupt#1): parks + opens ``paper_selection`` event and
  freezes until papers are chosen.
- ``review_gate`` (M3, interrupt#2): after every paper is analyzed, if any
  conclusion-cited evidence is machine-doubted it parks + opens the
  ``evidence_review_gate`` event and freezes before the final synthesis call.
  A recorded human decision routes back to ``analyze_papers`` (regenerate after
  review) or straight to ``synthesize`` (continue).

Checkpointing uses the same ``AsyncSqliteSaver`` instance as the search graph,
with a distinct thread prefix ``{project}:analysis:…``.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.analysis_nodes import (
    AnalysisGraphContext,
    make_acquire_documents_node,
    make_analyze_papers_node,
    make_review_gate_node,
    make_select_gate_node,
    make_synthesize_node,
)
from app.graph.analysis_state import AnalysisState


def _after_review_gate(state: AnalysisState) -> str:
    """Route back to the per-paper nodes after a review-triggered regeneration."""
    return "analyze_papers" if state.get("review_outcome") == "regenerated" else "synthesize"


# Durable-Execution: 组装分析图（选文门→获取→逐篇精读→复核门→合成，带 regenerate 回到精读的边）；与检索图共享同一 checkpointer，thread 前缀 {project}:analysis。
def build_analysis_graph(ctx: AnalysisGraphContext, checkpointer: Any | None = None) -> Any:
    """Compile the analysis graph with the given per-run context."""
    builder = StateGraph(AnalysisState)
    builder.add_node("select_gate", make_select_gate_node(ctx))
    builder.add_node("acquire_documents", make_acquire_documents_node(ctx))
    builder.add_node("analyze_papers", make_analyze_papers_node(ctx))
    builder.add_node("review_gate", make_review_gate_node(ctx))
    builder.add_node("synthesize", make_synthesize_node(ctx))
    builder.add_edge(START, "select_gate")
    builder.add_edge("select_gate", "acquire_documents")
    builder.add_edge("acquire_documents", "analyze_papers")
    builder.add_edge("analyze_papers", "review_gate")
    builder.add_conditional_edges(
        "review_gate",
        _after_review_gate,
        {"analyze_papers": "analyze_papers", "synthesize": "synthesize"},
    )
    builder.add_edge("synthesize", END)
    effective = checkpointer or getattr(ctx, "checkpointer", None)
    return builder.compile(checkpointer=effective)
