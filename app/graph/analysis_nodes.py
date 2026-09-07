"""analysis_graph nodes (M2).

- M1 moved the old ``_analyze_selected`` loop into graph nodes (behavior
  equivalent, no interrupts).
- M2 added the SelectGate: ``select_gate`` parks the project + opens the
  ``paper_selection`` hitl_event and ``interrupt()``s until the human picks
  papers (interrupt#1). M3 will add the ReviewGate before synthesis
  (interrupt#2).

Each node function is created by ``make_*_node(ctx)`` with a per-run context
(provider, repositories, document service / MCP capabilities …), mirroring how
search nodes are built as factories. Nodes write results to business tables
first and only then update graph state (idempotent, replay-safe); graph state
never carries heavy model objects.
"""

from __future__ import annotations

from types import SimpleNamespace

from langgraph.types import interrupt

from app.agents import EvidenceSynthesizer, PaperAnalyst
from app.agents.paper_analysis import PART_LABELS
from app.core.config import get_settings
from app.db import PaperRepository
from app.db.repositories import utc_now
from app.documents.acquisition import OpenAccessDownloader
from app.literature import LiteratureToolClient
from app.reliability.faults import AnalysisPausedError
from app.schemas import PaperAcquisition


class AnalysisJobStop(RuntimeError):
    """Graceful end of one analysis run *without* completing the project.

    Used when the run parked itself at a human wait (e.g. documents missing →
    ``document_unavailable`` event). The worker treats it as a normal job
    finish; a later human action enqueues a fresh job that re-enters the graph
    and the business-table idempotency skips whatever was already done.
    """


#: Per-run context bundle assembled by the workflow driver (see
#: ResearchWorkflowService._run_analysis_graph). Attributes consumed by nodes:
#: database, project_id, run_id, provider, projects, research, sessions, papers,
#: documents, evidence, transfer, traces, items, document_service, capabilities,
#: skill_registry, literature, checkpointer.
AnalysisGraphContext = SimpleNamespace


def _pause_message(reason: str, *, user: str, boundary: str) -> str:
    """Human-readable pause message for a safe-boundary stop."""
    if reason == "user":
        return user
    return (
        f"已达运行预算门槛（累计 token/视觉调用/时长超过阈值），"
        f"分析在{boundary}边界暂停，等待人工决定是否继续"
    )


async def _resolve_open_access_url(
    literature: LiteratureToolClient, metadata
) -> tuple[str | None, str | None]:
    """Return a public PDF candidate, enriching DOI-only records through MCP."""
    if metadata.open_access_url:
        return str(metadata.open_access_url), None
    if not metadata.doi:
        return None, "检索元数据没有开放获取链接或 DOI，无法自动定位公开 PDF。"
    try:
        enriched = await literature.get_paper_metadata(metadata.doi)
    except Exception as exc:  # noqa: BLE001 - manual upload remains the safe fallback
        return None, f"已尝试通过 DOI 补查公开全文，但元数据查询失败：{exc}"
    if enriched.open_access_url:
        return str(enriched.open_access_url), None
    return None, f"已通过 DOI {metadata.doi} 补查全文，但未发现开放获取 PDF。"


def _arxiv_pdf_url(metadata) -> str | None:
    """Return the canonical public PDF URL for an arXiv-sourced paper, if any."""
    arxiv_id = metadata.arxiv_id or (
        metadata.stable_id[len("arxiv:"):] if metadata.stable_id.startswith("arxiv:") else None
    )
    if not arxiv_id:
        return None
    identifier = arxiv_id.strip().rstrip("/").rsplit("/", 1)[-1]
    return f"https://arxiv.org/pdf/{identifier}"


def make_select_gate_node(ctx: AnalysisGraphContext):
    """M2 SelectGate (interrupt#1): a document_analysis run needs a confirmed
    selection before any PDF is downloaded.

    First pass without a selection: park the project (``waiting`` +
    ``paper_selection`` hitl_event) and ``interrupt()``, freezing the graph on
    this node. After the human picks papers, the resolving action saves the
    selection and enqueues a fresh analysis job: this node then reads the
    selection from the business table and proceeds (no LLM is spent on the
    wait). If a resumed thread still has no selection (e.g. search was
    regenerated while waiting), the node parks again and interrupts anew.
    """

    async def _park_and_prompt(candidates: int) -> None:
        await ctx.projects.wait_for_human(
            ctx.project_id,
            "paper_selection",
            event_type="paper_selection",
            title="请选择要精读的论文",
            reason=f"检索完成，候选 {candidates} 篇；确认前不会下载全文、不会触发分析。",
            options=[
                {"action": "select_papers", "label": "选择 1-2 篇并开始精读"},
                {"action": "regenerate_search", "label": "补充要求重新检索"},
            ],
            created_by="system",
            run_id=ctx.run_id,
        )

    async def select_gate_node(state) -> dict:
        while True:
            selection = await ctx.sessions.selection(ctx.project_id)
            if selection is not None:
                break
            candidates = 0
            search = await ctx.sessions.current_search(ctx.project_id)
            if search:
                candidates = len(
                    await ctx.sessions.current_paper_ids(
                        ctx.project_id, search["revision"]
                    )
                )
            await _park_and_prompt(candidates)
            # Freeze on this node until the human resolves the paper_selection
            # event (LangGraph interrupt; raising GraphInterrupt to the driver).
            await interrupt({
                "type": "paper_selection",
                "title": "请选择要精读的论文",
                "reason": "尚未选定论文；确认前不会下载全文、不会触发分析。",
                "options": [
                    {"action": "select_papers"},
                    {"action": "regenerate_search"},
                ],
                "default_action": None,
            })
            # Resumed (same-thread resume only): loop re-reads the selection.
        project = await ctx.projects.get(ctx.project_id)
        return {
            "search_revision": selection["search_revision"],
            "paper_ids": selection["paper_ids"],
            "requirements": selection.get("requirements"),
            "goal": project.goal,
        }

    return select_gate_node


def make_acquire_documents_node(ctx: AnalysisGraphContext):
    """Acquire + parse every selected paper's PDF (auto fetch, else park).

    Mirrors the old acquisition loop. When any paper still lacks a parseable
    PDF the run records the ``document_unavailable`` hitl_event and raises
    :class:`AnalysisJobStop` — the job ends gracefully and a later upload /
    reselect action re-enters the graph (already-parsed papers are skipped).
    """

    async def acquire_documents_node(state) -> dict:
        project_id = ctx.project_id
        transfer = ctx.transfer
        acquisitions = {
            item.paper_id: item
            for item in await transfer.list_acquisitions(project_id)
        }
        missing_ids: list[str] = []
        for paper_id in state["paper_ids"]:
            paper = await PaperRepository(ctx.database).get(project_id, paper_id)
            acquisition = acquisitions.get(paper_id)
            if acquisition and acquisition.status == "parsed":
                continue
            source_url, discovery_error = await _resolve_open_access_url(
                ctx.literature, paper.metadata
            )
            source_candidates: list[str] = []
            if source_url:
                source_candidates.append(source_url)
            arxiv_url = _arxiv_pdf_url(paper.metadata)
            if arxiv_url and arxiv_url not in source_candidates:
                source_candidates.append(arxiv_url)
            acquisition = PaperAcquisition(
                project_id=project_id, paper_id=paper_id, status="awaiting_upload",
                source_url=source_url, error=discovery_error, updated_at=utc_now(),
            )
            failures: list[str] = []
            for candidate in source_candidates:
                try:
                    await transfer.upsert_acquisition(
                        acquisition.model_copy(update={"status": "downloading"})
                    )
                    content, final_url = await OpenAccessDownloader(
                        ctx.document_service.workspace.max_document_bytes
                    ).fetch(candidate)
                    entry = ctx.document_service.workspace.import_pdf_bytes(
                        project_id, f"{paper_id}.pdf", content
                    )
                    await ctx.capabilities.parse_document(
                        project_id, entry.document_id, ctx.run_id
                    )
                    linked = await ctx.documents.register(project_id, paper_id, entry)
                    acquisition = acquisition.model_copy(update={
                        "status": "parsed", "source_url": final_url,
                        "document_id": linked.document_id, "updated_at": utc_now(),
                    })
                    break
                except Exception as exc:  # noqa: BLE001 - upload is the explicit fallback
                    failures.append(
                        f"{candidate}: {type(exc).__name__}: {str(exc)[:300]}"
                    )
                    acquisition = acquisition.model_copy(update={
                        "status": "awaiting_upload",
                        "error": "；".join(failures)[:1_000] if failures else discovery_error,
                        "updated_at": utc_now(),
                    })
            await transfer.upsert_acquisition(acquisition)
            if acquisition.status != "parsed":
                missing_ids.append(paper_id)
        if missing_ids:
            await ctx.projects.wait_for_human(
                project_id,
                "awaiting_documents",
                event_type="document_unavailable",
                title="部分论文缺少可解析的 PDF",
                reason="自动获取公开全文失败；上传可用的 PDF 后继续，其余已完成论文不受影响。",
                scope={"papers": missing_ids},
                options=[
                    {"action": "upload_documents", "label": "上传 PDF 后继续"},
                    {"action": "reselect_papers", "label": "换一批论文重新开始"},
                ],
                created_by="system",
                run_id=ctx.run_id,
            )
            raise AnalysisJobStop(
                "部分论文缺少可解析的 PDF，已暂停等待人工上传"
            )
        return {"documents_ready": True}

    return acquire_documents_node


def make_analyze_papers_node(ctx: AnalysisGraphContext):
    """Per-paper deep reading: visual observation / evidence index / the four
    specialists / overview / auto-verify — with pause checks on the existing
    safe boundaries (per paper, inside the analyst, before synthesis)."""

    async def analyze_papers_node(state) -> dict:
        project_id = ctx.project_id
        revision = state["search_revision"]
        await ctx.projects.set_stage(project_id, "analyzing_selected")
        analyst = PaperAnalyst(
            ctx.provider,
            ctx.document_service,
            ctx.documents,
            ctx.evidence,
            ctx.research,
            ctx.items,
            ctx.skill_registry,
            ctx.traces,
        )
        analyses = await ctx.sessions.paper_analyses(project_id, revision)
        completed = {item.paper_id for item in analyses}
        part_overrides = await ctx.sessions.part_instructions(project_id, revision)
        for paper_id in state["paper_ids"]:
            reason = await ctx.research.budget_pause_reason(
                project_id, get_settings()
            )
            if reason:
                raise AnalysisPausedError(
                    _pause_message(reason, user="用户请求在两篇论文之间暂停分析",
                                   boundary="两篇论文之间"),
                    reason=reason,
                )
            if paper_id in completed:
                continue
            paper = await ctx.papers.get(project_id, paper_id)
            linked = await ctx.documents.get_for_paper(project_id, paper_id)
            await ctx.capabilities.parse_document(
                project_id, linked.document_id, ctx.run_id
            )
            analysis = await analyst.run(
                project_id,
                revision,
                paper,
                linked,
                state["goal"],
                state.get("requirements"),
                trace_id=f"job-{ctx.run_id}",
                part_instructions=part_overrides.get(paper_id),
            )
            await ctx.sessions.save_paper_analysis(project_id, revision, analysis)
        return {"analyses_started": True}

    return analyze_papers_node


def make_synthesize_node(ctx: AnalysisGraphContext):
    """Final big call: cross-paper comparison + the synthesis report, then mark
    the project complete at ``analysis_review``.

    (M3 inserts the ReviewGate interrupt *before* this node.)
    """

    async def synthesize_node(state) -> dict:
        project_id = ctx.project_id
        revision = state["search_revision"]
        # Re-read analyses from the business table: everything persisted is in
        # scope, exactly like the old loop's accumulated list.
        analyses = await ctx.sessions.paper_analyses(project_id, revision)
        await ctx.research.set_progress(
            project_id, "", "synthesis", "running", label=PART_LABELS["synthesis"]
        )
        reason = await ctx.research.budget_pause_reason(project_id, get_settings())
        if reason:
            raise AnalysisPausedError(
                _pause_message(reason, user="用户请求在生成最终报告前暂停分析",
                               boundary="生成最终报告前"),
                reason=reason,
            )
        synthesizer = EvidenceSynthesizer(ctx.provider, ctx.sessions)
        await synthesizer.run(
            project_id, revision, state["goal"], state.get("requirements"), analyses
        )
        await ctx.research.set_progress(
            project_id, "", "synthesis", "completed", label=PART_LABELS["synthesis"]
        )
        await ctx.projects.complete(project_id, "analysis_review")
        return {"synthesis_completed": True}

    return synthesize_node


#: Claim fields of one PaperAnalysis that can carry ``evidence_ids`` (mirrors
#: app.evidence.review_desk.PAPER_CLAIM_FIELDS, kept local to avoid coupling).
_PAPER_CLAIM_FIELDS = (
    "core_problem", "methods", "mechanisms", "experimental_setup", "main_results",
    "limitations", "relevance_to_topic",
)


def _cited_evidence_ids(analysis) -> set[str]:
    """All evidence ids that supported conclusions of one analysis cite."""
    cited: set[str] = set()
    for field in _PAPER_CLAIM_FIELDS:
        for claim in getattr(analysis, field, None) or []:
            if getattr(claim, "kind", None) == "supported" and claim.evidence_ids:
                cited.update(claim.evidence_ids)
    overview = getattr(analysis, "overview", None)
    if overview is not None and getattr(overview, "kind", None) == "supported":
        cited.update(overview.evidence_ids or [])
    return cited


async def _high_risk_doubted_evidence_count(ctx: AnalysisGraphContext,
                                            project_id: str,
                                            revision: int) -> int:
    """Machine-doubted evidence that at least one supported conclusion cites.

    This is the decision-desk "high-risk segment" criterion (doubted ∧ cited),
    computed here pre-synthesis from the persisted paper analyses so the gate
    can decide whether the final (expensive) synthesis call deserves a human
    check first. Pure DB read — no tokens.
    """
    analyses = await ctx.sessions.paper_analyses(project_id, revision)
    cited: set[str] = set()
    for analysis in analyses:
        cited |= _cited_evidence_ids(analysis)
    if not cited:
        return 0
    rows = await ctx.research.list_reviews(project_id)
    return sum(
        1 for row in rows
        if row.get("status") == "doubted" and row.get("evidence_id") in cited
    )


def make_review_gate_node(ctx: AnalysisGraphContext):
    """M3 ReviewGate (interrupt#2): pre-synthesis human checkpoint.

    Placed after every paper is analyzed and before the final synthesis call:

    - No high-risk (machine-doubted, conclusion-cited) evidence → proceed.
    - A recorded decision exists (business-table truth, written by the UI
      action that resolved the previous wait):
        * ``continue`` → clear it and proceed to the synthesis.
        * ``regenerate_after_review`` → clear the stored paper analyses (so the
          per-paper nodes re-run with the adjudicated evidence pool — untouched
          parts stay cached in work_items) and loop back to ``analyze_papers``.
    - Otherwise → park the project (waiting + ``evidence_review_gate`` event +
      pause_reason='review_gate') and ``interrupt()`` until the human decides.
    """

    async def review_gate_node(state) -> dict:
        project_id = ctx.project_id
        revision = state["search_revision"]
        project = await ctx.projects.get(project_id)
        decision = project.analysis_review_decision
        if decision == "continue":
            await ctx.projects.set_analysis_review_decision(project_id, None)
            return {"review_outcome": "continue"}
        if decision == "regenerate_after_review":
            # Human adjudicated the flagged evidence (possibly excluded some).
            # Clear the per-paper analyses so the graph re-runs them against the
            # filtered evidence pool; work_item caches keep untouched parts
            # token-free, and _sanitize_evidence drops claims whose only source
            # was excluded.
            await ctx.sessions.reset_analysis(project_id, revision)
            await ctx.projects.set_analysis_review_decision(project_id, None)
            return {"review_outcome": "regenerated"}
        high_risk = await _high_risk_doubted_evidence_count(
            ctx, project_id, revision
        )
        if high_risk == 0:
            return {"review_outcome": "continue"}
        await ctx.projects.set_pause_reason(project_id, "review_gate")
        await ctx.projects.wait_for_human(
            project_id,
            "analysis_paused",
            event_type="evidence_review_gate",
            title=f"有 {high_risk} 条被引用的自动存疑证据，合成前先复核？",
            reason=(
                "这些证据被已有结论引用但自动复核判定存疑；综合报告是最后一大笔调用，"
                "先处理争议证据可避免为注定要改的结论付费。"
            ),
            options=[
                {"action": "review_gate_continue", "label": "直接生成报告（跳过复核）"},
                {"action": "review_gate_regenerate", "label": "先处理争议证据再重新生成"},
            ],
            created_by="system",
            run_id=ctx.run_id,
        )
        await interrupt({
            "type": "evidence_review_gate",
            "title": "合成前证据复核门",
            "reason": "存在被引用且自动存疑的证据；可选择直接生成或先处理争议证据。",
            "options": [
                {"action": "review_gate_continue"},
                {"action": "review_gate_regenerate"},
            ],
            "default_action": None,
        })
        # Same-thread resume without a recorded decision: re-evaluate.
        return {"review_outcome": "continue"}

    return review_gate_node
