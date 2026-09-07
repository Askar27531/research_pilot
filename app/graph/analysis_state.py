"""State channels for the analysis LangGraph (graph ②, M1 skeleton).

Heavy results never live here: regions / evidence / work items / paper analyses /
reports are persisted in the business tables first (idempotent anchors), and the
graph state only carries the orchestration ledger (ids, flags, small context) so
checkpoints stay small and replay is safe.
"""

from typing import Any, TypedDict


class AnalysisState(TypedDict, total=False):
    # —— read-only run context (filled by ensure_selection_node) ——
    project_id: str
    search_revision: int
    goal: str
    requirements: str | None
    paper_ids: list[str]
    # —— orchestration ledger ——
    documents_ready: bool  # acquire_documents_node finished without missing PDFs
    analyses_started: bool
    synthesis_completed: bool
    # —— future interrupt/resume channels (M2/M3) ——
    review_gate: dict[str, Any] | None
    review_outcome: str | None
