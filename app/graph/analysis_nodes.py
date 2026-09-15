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
from app.documents.acquisition import OpenAccessDownloader, SourceGoneError
from app.literature import LiteratureToolClient
from app.literature.open_access import open_access_candidates
from app.reliability.faults import AnalysisPausedError
from app.schemas import OpenAccessLocation, PaperAcquisition, PaperMetadata


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


def _metadata_failure_reason(exc: Exception) -> str:
    """Short, user-facing reason for a failed DOI metadata lookup.

    The raw error is an MCP routing aggregate ("No MCP route completed
    literature.metadata (tried: …)") and must not reach the UI. Callers keep the
    untouched text in the run trace for diagnosis.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(str(item) for item in chain).casefold()
    if "api_key" in text or "auth" in text:
        return "元数据服务未配置或密钥无效"
    if "rate limit" in text or "429" in text:
        return "元数据服务请求过于频繁，请稍后重试"
    if any(word in text for word in ("reach", "timeout", "timed out", "connect", "endofstream")):
        return "元数据服务暂时无法连接"
    if "not found" in text or "no metadata source has" in text:
        return "该 DOI 在各元数据源中均未收录"
    return "元数据服务暂时不可用"


async def _discover_open_access(
    literature: LiteratureToolClient, metadata
) -> tuple[list[str], str | None, str | None]:
    """Return ``(candidate_urls, user_message, raw_detail)``.

    Candidates come back ordered most-likely-to-download first (repository and
    preprint copies before publisher pages). ``raw_detail`` carries the untouched
    provider error so the caller can record it in the run trace, while
    ``user_message`` is what the upload screen renders.
    """
    candidates = open_access_candidates(metadata)
    if candidates:
        return candidates, None, None
    if not metadata.doi:
        return [], "检索元数据没有开放获取链接或 DOI，无法自动定位公开 PDF。", None
    try:
        enriched = await literature.get_paper_metadata(metadata.doi)
    except Exception as exc:  # noqa: BLE001 - manual upload remains the safe fallback
        return (
            [],
            (
                f"已尝试通过 DOI 补查公开全文，但{_metadata_failure_reason(exc)}，"
                "可稍后重试或手动上传 PDF。"
            ),
            f"{type(exc).__name__}: {exc}",
        )
    refreshed = open_access_candidates(enriched)
    if refreshed:
        return refreshed, None, None
    return [], f"已通过 DOI {metadata.doi} 补查全文，但未发现开放获取 PDF。", None


async def _expand_open_access(
    literature: LiteratureToolClient, metadata, already: set[str]
) -> tuple[list[str], str | None]:
    """Re-ask the metadata service once every stored candidate has failed.

    Papers stored before full location capture carry only the single
    ``open_access_url`` — typically the publisher page that just answered 403.
    Without this second look the repository copies of the same paper would never
    be seen at all.

    Returns newly discovered URLs plus a raw failure detail for the trace.
    """
    if not metadata.doi:
        return [], None
    try:
        enriched = await literature.get_paper_metadata(metadata.doi)
    except Exception as exc:  # noqa: BLE001 - manual upload remains the fallback
        return [], f"{type(exc).__name__}: {exc}"
    return [url for url in open_access_candidates(enriched) if url not in already], None


async def _attempt_downloads(
    ctx: AnalysisGraphContext,
    project_id: str,
    paper_id: str,
    urls: list[str],
    attempted: set[str],
    failures: list[str],
    stale_urls: set[str],
    acquisition: PaperAcquisition,
    fallback_error: str | None,
) -> PaperAcquisition:
    """Download the first URL that works, returning the updated acquisition row."""
    for candidate in urls:
        if candidate in attempted:
            continue
        attempted.add(candidate)
        try:
            await ctx.transfer.upsert_acquisition(
                acquisition.model_copy(update={"status": "downloading"})
            )
            content, final_url = await OpenAccessDownloader(
                ctx.document_service.workspace.max_document_bytes
            ).fetch(candidate)
            entry = ctx.document_service.workspace.import_pdf_bytes(
                project_id, f"{paper_id}.pdf", content
            )
            await ctx.capabilities.parse_document(project_id, entry.document_id)
            linked = await ctx.documents.register(project_id, paper_id, entry)
            return acquisition.model_copy(update={
                "status": "parsed", "source_url": final_url,
                "document_id": linked.document_id, "updated_at": utc_now(),
            })
        except Exception as exc:  # noqa: BLE001 - upload is the explicit fallback
            if isinstance(exc, SourceGoneError):
                # Permanently gone (404/410): remember it so the stored record can
                # be corrected and later runs stop re-trying this URL.
                stale_urls.add(candidate)
            failures.append(f"{candidate}: {type(exc).__name__}: {str(exc)[:300]}")
            acquisition = acquisition.model_copy(update={
                "status": "awaiting_upload",
                "error": "；".join(failures)[:1_000] if failures else fallback_error,
                "updated_at": utc_now(),
            })
    return acquisition


def _mark_locations_stale(
    metadata: PaperMetadata, stale_urls: set[str]
) -> PaperMetadata | None:
    """Flag locations whose URL proved permanently gone.

    Returns the corrected metadata, or ``None`` when nothing needed changing, so
    the caller can skip a pointless database write.
    """
    if not stale_urls:
        return None
    locations: list[OpenAccessLocation] = []
    changed = False
    for location in metadata.oa_locations:
        if location.url in stale_urls and not location.stale:
            locations.append(location.model_copy(update={"stale": True}))
            changed = True
        else:
            locations.append(location)
    legacy = str(metadata.open_access_url) if metadata.open_access_url else None
    # A stale legacy URL must be cleared too, otherwise open_access_candidates
    # would simply re-add it as the single-URL compatibility candidate.
    clear_legacy = legacy is not None and legacy in stale_urls
    if changed and clear_legacy:
        return metadata.model_copy(
            update={"oa_locations": locations, "open_access_url": None}
        )
    if changed:
        return metadata.model_copy(update={"oa_locations": locations})
    if clear_legacy:
        return metadata.model_copy(update={"open_access_url": None})
    return None


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

    # Durable-Execution: 选文门(interrupt#1)：无选文时先落业务表(wait_for_human 置 waiting+open 事件)再 interrupt() 冻结图，等待期零 LLM 占用；人工 resolve 后同线程续跑，循环重读业务表放行（重新生成搜索则再次 park）。
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
            candidates, discovery_error, discovery_detail = await _discover_open_access(
                ctx.literature, paper.metadata
            )
            if discovery_detail:
                # Keep the provider/MCP detail in the trace so the UI stays plain.
                await ctx.traces.append(
                    project_id,
                    f"discovery-{paper_id}",
                    "open_access_discovery_failed",
                    success=False,
                    agent="analysis_graph",
                    summary={"paper_id": paper_id, "doi": paper.metadata.doi},
                    error={"type": "metadata_lookup", "message": discovery_detail[:1_000]},
                )
            acquisition = PaperAcquisition(
                project_id=project_id, paper_id=paper_id, status="awaiting_upload",
                source_url=candidates[0] if candidates else None,
                error=discovery_error, updated_at=utc_now(),
            )
            failures: list[str] = []
            attempted: set[str] = set()
            stale_urls: set[str] = set()
            acquisition = await _attempt_downloads(
                ctx, project_id, paper_id, candidates, attempted, failures,
                stale_urls, acquisition, discovery_error,
            )
            if acquisition.status != "parsed":
                # Every stored candidate failed. Rows stored before full location
                # capture only ever carried one URL (usually the publisher page
                # that just answered 403), so look once more for the repository
                # copies that were never recorded.
                extra, refresh_detail = await _expand_open_access(
                    ctx.literature, paper.metadata, attempted
                )
                if refresh_detail:
                    await ctx.traces.append(
                        project_id,
                        f"expansion-{paper_id}",
                        "open_access_expansion_failed",
                        success=False,
                        agent="analysis_graph",
                        summary={"paper_id": paper_id, "doi": paper.metadata.doi},
                        error={"type": "metadata_lookup", "message": refresh_detail[:1_000]},
                    )
                if extra:
                    await ctx.traces.append(
                        project_id,
                        f"expansion-{paper_id}",
                        "open_access_candidates_expanded",
                        success=True,
                        agent="analysis_graph",
                        summary={"paper_id": paper_id, "found": len(extra), "urls": extra[:5]},
                    )
                    acquisition = await _attempt_downloads(
                        ctx, project_id, paper_id, extra, attempted, failures,
                        stale_urls, acquisition, discovery_error,
                    )
            corrected = _mark_locations_stale(paper.metadata, stale_urls)
            if corrected is not None:
                # Correct the stored record so a dead link is not re-tried on the
                # next run (OpenAlex keeps serving the same stale URL).
                await PaperRepository(ctx.database).save_metadata(
                    project_id, paper_id, corrected
                )
                await ctx.traces.append(
                    project_id,
                    f"stale-{paper_id}",
                    "open_access_locations_pruned",
                    success=False,
                    agent="analysis_graph",
                    summary={"paper_id": paper_id, "stale": sorted(stale_urls)[:5]},
                )
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
            await ctx.capabilities.parse_document(project_id, linked.document_id)
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
