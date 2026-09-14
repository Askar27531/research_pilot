import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.api.tokens import issue_token, read_token
from app.core.config import get_settings
from app.db import (
    DocumentRepository,
    EvidenceRepository,
    HitlEventRepository,
    PaperRepository,
    ProjectRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    TransferRepository,
    WorkflowJobRepository,
    WorkItemRepository,
)
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import utc_now
from app.documents import DocumentValidationError
from app.evidence import EvidenceVerifier
from app.evidence.review_desk import (
    build_queue,
    citation_index,
    citation_index_papers,
    filter_rows,
    impact_preview,
)
from app.schemas import (
    DeskClaimRef,
    PaperAcquisition,
    ProjectSummary,
    ProjectWorkspace,
    ResearchRequest,
    ReviewDesk,
    ReviewDeskItem,
    ReviewDeskStats,
    ReviewPreviewResult,
    WorkspaceActionRequest,
    WorkspaceMutationResult,
    WorkspaceProjectCreate,
    WorkspaceProjectUpdate,
)
from app.schemas.review import ReviewPreviewRequest
from app.schemas.workspace import (
    MissingDocument,
    RetryAction,
    UploadDocumentsAction,
    WaitAction,
    WorkspaceProgress,
)


async def _optional(awaitable):
    try:
        return await awaitable
    except RecordNotFoundError:
        return None


def _split_constraints(constraints: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Reverse the flat ``ResearchRequest.constraints`` list back into the three
    edit-form fields it was built from (see ``_project_inputs``)."""
    approach: str | None = None
    difficulties: list[str] = []
    metrics: list[str] = []
    for item in constraints:
        if item.startswith("Current approach: "):
            approach = item[len("Current approach: "):]
        elif item.startswith("Target metric: "):
            metrics.append(item[len("Target metric: "):])
        else:
            difficulties.append(item)
    return approach, difficulties, metrics


#: Part ordering + which report fields each per-paper part owns. These mirror the
#: specialist blocks in app.agents.paper_analysis so the incremental analysis
#: surface can rebuild a per-part view straight from the persisted work items.
_ANALYSIS_PART_ORDER = ("problem", "method", "experiment", "critical", "overview")
_ANALYSIS_PART_FIELDS: dict[str, tuple[str, ...]] = {
    "problem": ("core_problem", "relevance_to_topic"),
    "method": ("methods", "mechanisms"),
    "experiment": ("experimental_setup", "main_results"),
    "critical": ("limitations",),
    "overview": ("overview",),
}
_ANALYSIS_PART_LABELS: dict[str, str] = {
    "problem": "问题与贡献",
    "method": "方法与机制",
    "experiment": "实验与结果",
    "critical": "局限与相关性",
    "overview": "综合概述",
}


def _walk_evidence_ids(value) -> set[str]:
    """Collect every ``evidence_ids`` list in a (dict/list) payload tree."""
    ids: set[str] = set()

    def collect(item):
        if isinstance(item, dict):
            if "evidence_ids" in item:
                ids.update(item["evidence_ids"])
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return ids


def _replace_evidence_ids(value, evidence_by_id, resource_url) -> None:
    """In-place: swap each ``evidence_ids`` list for inline evidence objects."""
    if isinstance(value, dict):
        if "evidence_ids" in value:
            value["evidence"] = [
                {
                    "type": evidence_by_id[identifier].evidence_type,
                    "page": evidence_by_id[identifier].page_number,
                    "section": evidence_by_id[identifier].section,
                    "label": evidence_by_id[identifier].label,
                    "claim": evidence_by_id[identifier].claim,
                    "excerpt": evidence_by_id[identifier].excerpt,
                    "resource_url": resource_url(identifier),
                }
                for identifier in value.pop("evidence_ids")
                if identifier in evidence_by_id
            ]
        for child in value.values():
            _replace_evidence_ids(child, evidence_by_id, resource_url)
    elif isinstance(value, list):
        for child in value:
            _replace_evidence_ids(child, evidence_by_id, resource_url)


@dataclass(frozen=True)
class WorkspaceResource:
    content: bytes
    media_type: str
    filename: str | None = None


class WorkspaceService:
    """Application boundary used by the consolidated workspace API."""

    def __init__(self, app, database=None) -> None:
        self.app = app
        database = database or app.state.database
        self.projects = ProjectRepository(database)
        self.papers = PaperRepository(database)
        self.transfers = TransferRepository(database)
        self.items = WorkItemRepository(database)
        self.jobs = WorkflowJobRepository(database)
        self.evidence = EvidenceRepository(database)
        self.research = ResearchDataRepository(database)
        self.sessions = ResearchSessionRepository(database)
        self.documents = DocumentRepository(database)
        self.hitl = HitlEventRepository(database)
        self.document_service = app.state.document_service

    @staticmethod
    def stage(project, job=None) -> tuple[str, str, str]:
        if job is not None and job.status in {"queued", "running"}:
            if job.job_type == "research":
                return "searching", "正在检索", "大模型正在生成策略并通过 MCP 检索文献。"
            if project.current_stage == "analyzing_selected":
                return "analyzing_selected", "正在分析所选论文", "正在解析全文、图表并整理证据。"
            return "acquiring_selected", "正在获取全文", "只获取你选择的论文全文。"
        if project.status == "failed":
            detail = (project.error or {}).get("message", "研究任务未完成。")
            return "failed", "需要重试", detail
        if project.status == "created":
            return "setup", "准备开始", "课题已保存，可以开始论文研究。"
        mapping = {
            "paper_selection": (
                "paper_selection", "请选择论文", "检索已完成，请选择一至两篇论文。"
            ),
            "acquiring_selected": (
                "acquiring_selected", "正在获取全文", "只获取你选择的论文全文。"
            ),
            "analyzing_selected": (
                "analyzing_selected", "正在分析所选论文", "正在解析全文、图表并整理证据。"
            ),
            "analysis_review": (
                "analysis_review", "分析完成", "证据化论文综合分析已经生成。"
            ),
            "awaiting_documents": (
                "documents_needed", "需要补充全文", "部分关键论文需要上传 PDF。"
            ),
        }
        if project.current_stage == "analysis_paused":
            # A budget-gate pause carries a different message: the project was
            # stopped automatically at a safe boundary once the metered usage
            # crossed a threshold, and "continue" re-baselines the allowance.
            if project.pause_reason == "budget_gate":
                return (
                    "paused", "分析已暂停（成本门槛）",
                    (
                        "累计调用量达到预算门槛，分析已在安全边界自动停下；已完成结果全部保留。"
                        "可继续分析（放行剩余部分并重新开始计量），或暂不操作保持暂停。"
                    ),
                )
            if project.pause_reason == "review_gate":
                return (
                    "paused", "分析已暂停（证据复核）",
                    (
                        "合成前存在被引用且自动存疑的证据，等待你决定："
                        "直接生成报告，或先处理争议证据再重新生成。"
                    ),
                )
            return (
                "paused", "分析已暂停",
                "已在安全边界停下；可继续分析，或对某篇某部分补充要求后只重跑该部分。",
            )
        if project.current_stage in mapping:
            return mapping[project.current_stage]
        if project.status == "completed":
            return "analysis_review", "分析完成", "证据化论文综合分析已经生成。"
        return "searching", "正在准备", "正在准备下一阶段。"

    async def list_projects(self) -> list[ProjectSummary]:
        projects = await self.projects.list()
        jobs = await self.jobs.latest_for_projects([project.id for project in projects])
        result = []
        for project in projects:
            stage, label, _ = self.stage(project, jobs.get(project.id))
            result.append(ProjectSummary(
                id=project.id,
                name=project.name,
                user_stage=stage,
                status_label=label,
                updated_at=project.updated_at,
            ))
        return result

    async def create(self, body: WorkspaceProjectCreate) -> WorkspaceMutationResult:
        request = self._project_inputs(body)
        project_id, job_id = await self.research.create_workspace(
            body.project_name or body.research_question[:120], request
        )
        await self.sessions.begin_search(project_id)
        job = await self.jobs.get(job_id)
        self.app.state.workflow_worker.wake()
        return WorkspaceMutationResult(
            message="项目已创建，正在开始论文研究",
            workspace=await self.workspace(project_id),
            job=job,
        )

    async def update(
        self, project_id: str, body: WorkspaceProjectUpdate
    ) -> WorkspaceMutationResult:
        await self.app.state.workflow_worker.cancel_project(project_id)
        request = self._project_inputs(body)
        await self.projects.update_definition(project_id, body.project_name, request)
        return WorkspaceMutationResult(
            message="课题设置已保存，可前往检索文献阶段重新检索",
            workspace=await self.workspace(project_id),
        )

    @staticmethod
    def _project_inputs(body: WorkspaceProjectCreate) -> ResearchRequest:
        return ResearchRequest(
            research_question=body.research_question,
            year_from=body.advanced.year_from,
            year_to=body.advanced.year_to,
            maximum_papers=body.advanced.max_papers,
            constraints=[
                *([f"Current approach: {body.current_approach}"] if body.current_approach else []),
                *body.difficulties,
                *[f"Target metric: {metric}" for metric in body.target_metrics],
            ],
            literature_sources=body.advanced.sources,
        )

    async def delete(self, project_id: str) -> None:
        # The worker must stop first; otherwise it could recreate project output after deletion.
        await self.projects.get(project_id)
        await self.app.state.workflow_worker.cancel_project(project_id)
        await self.projects.delete(project_id)
        self.document_service.workspace.delete_project(project_id)

    async def workspace(self, project_id: str, paper_token: str | None = None) -> ProjectWorkspace:
        search = await self.sessions.current_search(project_id)
        revision = search["revision"] if search else 0
        selection = await self.sessions.selection(project_id)
        report = await self.sessions.report(project_id, revision) if revision else None
        # Scope the analysis metrics / "current visual" to the current selection's
        # documents so a superseded paper's still-running pass never leaks into
        # the page/figure counts: a reselect must read as a fresh, isolated run.
        allowed_documents: list[str] | None = None
        if selection:
            allowed_documents = []
            for paper_id in selection["paper_ids"]:
                linked = await _optional(self.documents.get_for_paper(project_id, paper_id))
                if linked:
                    allowed_documents.append(linked.document_id)
        (
            project,
            job,
            metrics,
            records,
            acquisition_list,
            analysis_metrics,
            review_counts,
            usage,
            budget_win,
            hitl_events,
            running_visual,
        ) = await asyncio.gather(
            self.projects.get(project_id),
            self.jobs.latest_for_project(project_id),
            self.items.metrics(project_id),
            self.papers.list_for_project(project_id),
            self.transfers.list_acquisitions(project_id),
            self.research.analysis_metrics(project_id, allowed_documents),
            self.research.review_counts(project_id),
            self.research.usage_totals(project_id),
            self.research.budget_window(project_id),
            self.hitl.open_events(project_id),
            self.research.running_visual_analysis(project_id, allowed_documents),
        )
        user_stage, label, detail = self.stage(project, job)
        acquisitions = {value.paper_id: value for value in acquisition_list}
        current_ids = set(
            await self.sessions.current_paper_ids(project_id, revision)
        ) if revision else set()
        records = [paper for paper in records if paper.id in current_ids]
        literature = [self._paper_card(project_id, revision, paper, acquisitions.get(paper.id))
                      for paper in records]
        selected_paper = await self._paper_detail(
            project_id, paper_token, records, acquisitions
        ) if paper_token else None
        next_action = self._next_action(project_id, user_stage, detail, records, acquisitions)
        current_visual = (
            await self._current_visual(project_id, running_visual)
        ) if running_visual else None
        high_risk_review_count = 0
        if report is not None:
            # Cheap banner count (no evidence fetch): machine-doubted evidence
            # that at least one conclusion in the report cites.
            try:
                index = citation_index(report)
                reviews = {row["evidence_id"]: row
                           for row in await self.research.list_reviews(project_id)}
                high_risk_review_count = sum(
                    1 for row in reviews.values()
                    if row.get("status") == "doubted" and row["evidence_id"] in index
                )
            except Exception:  # noqa: BLE001 - the banner must never break the page
                high_risk_review_count = 0
        budget_hint = None
        running_analysis = (
            user_stage in {"acquiring_selected", "analyzing_selected"} and job is not None
        )
        budget_paused = (
            user_stage == "paused" and project.pause_reason == "budget_gate"
        )
        if running_analysis or budget_paused:
            budget_hint = self._budget_hint(job, analysis_metrics, usage, budget_win)
        board: list[dict[str, Any]] = []
        if selection:
            allowed_papers = set(selection["paper_ids"])
            titles = {value.id: value.metadata.title for value in records}
            for row in await self.research.list_progress(project_id):
                if row["paper_id"] and row["paper_id"] not in allowed_papers:
                    continue
                board.append({
                    "paper_id": row["paper_id"],
                    "paper_title": (
                        titles.get(row["paper_id"]) if row["paper_id"] else None
                    ),
                    "stage_key": row["stage_key"],
                    "label": row["label"],
                    "status": row["status"],
                    "done": row["done"],
                    "total": row["total"],
                    "updated_at": row["updated_at"],
                })
        analysis_papers = await self._analysis_papers(
            project_id, revision, selection, records
        )
        analysis_blocks = await self._analysis_blocks(
            project_id, revision, selection, records, board
        )
        approach, difficulties, metrics = _split_constraints(project.request.constraints)
        return ProjectWorkspace(
            project_id=project.id,
            name=project.name,
            user_stage=user_stage,
            status_label=label,
            status_detail=detail,
            next_action=next_action,
            progress=WorkspaceProgress(
                found_papers=len(records),
                full_text_papers=sum(1 for value in acquisitions.values()
                    if value.status in {"parsed", "acquired"}),
                analyzed_pages=analysis_metrics["analyzed_pages"],
                analyzed_visuals=analysis_metrics["analyzed_visuals"],
                completed_items=metrics.completed_items,
                running_items=metrics.running_items,
                failed_items=metrics.failed_items,
                total_visuals=int(analysis_metrics["total_visuals"] or 0),
                completed_visuals=int(analysis_metrics["completed_visuals"] or 0),
                current_step=(
                    str(analysis_metrics["current_step"])
                    if analysis_metrics["current_step"] else None
                ),
                current_visual=current_visual,
            ),
            literature=literature,
            selected_paper=selected_paper,
            search_plan=search.get("plan") if search else None,
            search_revision=revision,
            selected_paper_tokens=[
                issue_token(project_id, "paper", f"{revision}:{paper_id}")
                for paper_id in (selection or {}).get("paper_ids", [])
            ],
            analysis_requirements=(selection or {}).get("requirements"),
            analysis_report=await self._report_payload(project_id, report) if report else None,
            analysis_papers=analysis_papers,
            analysis_blocks=analysis_blocks,
            evidence_review=review_counts,
            high_risk_review_count=high_risk_review_count,
            budget_hint=budget_hint,
            waiting_for_human=project.status == "waiting" and bool(hitl_events),
            hitl_events=hitl_events,
            analysis_board=board,
            diagnostics={
                "project_status": project.status,
                "current_stage": project.current_stage,
                "project_version": project.version,
                "job": job.model_dump(mode="json") if job else None,
            },
            project_input={
                "project_name": project.name,
                "research_question": project.request.research_question,
                "current_approach": approach,
                "difficulties": difficulties,
                "target_metrics": metrics,
                "advanced": {
                    "year_from": project.request.year_from,
                    "year_to": project.request.year_to,
                    "max_papers": project.request.maximum_papers,
                    "sources": project.request.literature_sources,
                },
            },
        )

    async def _current_visual(self, project_id: str, running: dict) -> dict | None:
        """Which visual is being analyzed right now.

        Rebuilds the in-flight document's ordered visual list from the cached
        parse (figures then tables), compares against the already-persisted
        ``visual_regions`` keys, and reports the first not-yet-done visual with
        its page + figure/table ordinal + per-kind counts. Best-effort: any
        parse/IO issue returns None so the UI falls back to plain counts.
        """
        try:
            parsed = self.document_service.get_structure(
                project_id, running["document_id"]
            )
        except Exception:  # noqa: BLE001 - cosmetic; fall back to plain counts
            return None
        visuals = [
            {"key": f"{v.page_number}|figure|{v.label or ''}",
             "page": v.page_number, "kind": "figure", "label": v.label}
            for v in parsed.figures
        ] + [
            {"key": f"{v.page_number}|table|{v.label or ''}",
             "page": v.page_number, "kind": "table", "label": v.label}
            for v in parsed.tables
        ]
        if not visuals:
            return None
        existing = await self.research.visual_keys(running["id"])
        kind_total = {
            "figure": sum(1 for v in visuals if v["kind"] == "figure"),
            "table": sum(1 for v in visuals if v["kind"] == "table"),
        }
        done: dict[str, int] = {"figure": 0, "table": 0}
        for index, visual in enumerate(visuals):
            if visual["key"] in existing:
                done[visual["kind"]] += 1
                continue
            return {
                "page": visual["page"],
                "kind": visual["kind"],
                "label": visual["label"],
                "index": index + 1,
                "total": len(visuals),
                "kind_index": done[visual["kind"]] + 1,
                "kind_total": kind_total[visual["kind"]],
                "kind_done": done[visual["kind"]],
            }
        return None

    def _paper_card(self, project_id, revision, paper, acquisition) -> dict:        return {
            "paper_token": issue_token(project_id, "paper", f"{revision}:{paper.id}"),
            "title": paper.metadata.title,
            "authors": [author.name for author in paper.metadata.authors],
            "year": paper.metadata.year,
            "abstract": paper.metadata.abstract,
            "sources": paper.metadata.sources or [paper.metadata.source],
            "relevance": paper.relevance_score,
            "reason": paper.selection_reason,
            "full_text": acquisition.status if acquisition else None,
        }

    async def _paper_detail(self, project_id, paper_token, records, acquisitions) -> dict:
        encoded = read_token(paper_token, project_id, "paper")["i"]
        revision_text, paper_id = encoded.split(":", 1)
        current = await self.sessions.current_search(project_id)
        if current is None or int(revision_text) != current["revision"]:
            raise RecordNotFoundError("Paper belongs to an outdated search")
        # The token was issued by the server against this search's own records,
        # so revision equality (checked above) already guarantees membership.
        paper = next(value for value in records if value.id == paper_id)
        nodes = await self.evidence.list_for_paper(project_id, paper_id)
        acquisition = acquisitions.get(paper_id)
        regions = (
            await self.research.visual_regions_for_document(project_id, acquisition.document_id)
            if acquisition and acquisition.document_id else []
        )
        return {
            "paper_token": paper_token,
            "title": paper.metadata.title,
            "metadata": paper.metadata.model_dump(mode="json"),
            "evidence": [self._evidence_card(project_id, node) for node in nodes],
            "figures": [self._visual_card(project_id, region) for region in regions
                        if region["region_type"] == "figure"],
            "tables": [self._visual_card(project_id, region) for region in regions
                       if region["region_type"] == "table"],
        }

    def _evidence_card(self, project_id, node) -> dict:
        token = issue_token(project_id, "evidence", node.evidence_id)
        return {
            "evidence_token": token,
            "type": node.evidence_type,
            "claim": node.claim,
            "confidence": node.confidence,
            "page": node.page_number,
            "label": node.label,
            "resource_url": self._resource_url(project_id, "evidence", node.evidence_id),
        }

    def _visual_card(self, project_id, region) -> dict:
        return {
            "label": region["label"],
            "page": region["page_number"],
            "bbox": region["bbox"],
            "analysis": region["payload"],
            "resource_url": self._resource_url(project_id, "visual", region["id"]),
        }

    async def _report_payload(self, project_id, report) -> dict:
        payload = report.model_dump(mode="json")
        linked_documents = await asyncio.gather(*(
            _optional(self.documents.get_for_paper(project_id, paper.paper_id))
            for paper in report.papers
        ))
        for paper_payload, linked in zip(
            payload["papers"], linked_documents, strict=True
        ):
            paper_payload["pdf_url"] = (
                self._resource_url(project_id, "document", linked.document_id)
                if linked else None
            )
        await self._inline_payload_evidence(project_id, payload)
        return payload

    async def _inline_payload_evidence(self, project_id, payload) -> None:
        """In-place: turn every claim's ``evidence_ids`` into inline evidence."""
        lookup = lambda identifier: self._resource_url(
            project_id, "evidence", identifier
        )
        identifiers = _walk_evidence_ids(payload)
        nodes = await asyncio.gather(*(
            _optional(self.evidence.get(project_id, identifier))
            for identifier in identifiers
        ))
        evidence_by_id = {node.evidence_id: node for node in nodes if node is not None}
        _replace_evidence_ids(payload, evidence_by_id, lookup)

    async def _analysis_papers(self, project_id, revision, selection, records):
        """Selected papers + live PDF handles (available before synthesis)."""
        if not selection:
            return []
        out: list[dict] = []
        for paper_id in selection["paper_ids"]:
            paper = next((value for value in records if value.id == paper_id), None)
            if paper is None:
                continue
            linked = await _optional(self.documents.get_for_paper(project_id, paper_id))
            out.append({
                "paper_id": paper_id,
                "paper_token": issue_token(project_id, "paper", f"{revision}:{paper_id}"),
                "title": paper.metadata.title,
                "pdf_url": (
                    self._resource_url(project_id, "document", linked.document_id)
                    if linked else None
                ),
            })
        return out

    async def _analysis_blocks(self, project_id, revision, selection, records, board):
        """Per-(paper, part) analysis blocks for incremental rendering.

        Builds the part content from the persisted ``paper_analysis_section``
        work items (so it is visible as soon as a section completes) and marks
        each part's state from ``analysis_progress`` (queued / running /
        completed). This lets the UI stream content while the analysis runs.
        """
        if not selection:
            return []
        titles = {value.id: value.metadata.title for value in records}
        status_by_paper: dict[str, dict[str, str]] = {}
        for row in board:
            status_by_paper.setdefault(row["paper_id"], {})[row["stage_key"]] = row["status"]
        blocks: list[dict] = []
        part_instructions = await self.sessions.part_instructions(project_id, revision)
        for paper_id in selection["paper_ids"]:
            paper_status = status_by_paper.get(paper_id, {})
            sections = await self.items.analysis_sections(project_id, revision, paper_id)
            for part in _ANALYSIS_PART_ORDER:
                content = None
                if part in sections:
                    draft = sections[part]
                    content = {
                        field: draft.get(field)
                        for field in _ANALYSIS_PART_FIELDS[part]
                        if draft.get(field)
                    }
                    if content:
                        await self._inline_payload_evidence(project_id, content)
                # Status is content-derived first (a section's result is
                # authoritative even for projects analysed before the
                # analysis_progress table existed); otherwise fall back to the
                # live progress board, then to "queued".
                if content:
                    status = "completed"
                else:
                    status = paper_status.get(part, "queued")
                blocks.append({
                    "paper_id": paper_id,
                    "paper_token": issue_token(
                        project_id, "paper", f"{revision}:{paper_id}"
                    ),
                    "paper_title": titles.get(paper_id, "论文"),
                    "part_key": part,
                    "part_label": _ANALYSIS_PART_LABELS[part],
                    "status": status,
                    "content": content,
                    "note": part_instructions.get(paper_id, {}).get(part),
                })
        return blocks

    @staticmethod
    def _resource_url(project_id: str, kind: str, identifier: str) -> str:
        return f"/projects/{project_id}/resources/{issue_token(project_id, kind, identifier)}"

    def _next_action(self, project_id, stage, detail, records, acquisitions):
        if stage in {"searching", "paper_selection", "acquiring_selected", "analyzing_selected",
                     "analysis_review"}:
            return WaitAction(job_id=None)
        if stage == "documents_needed":
            return UploadDocumentsAction(documents=[
                MissingDocument(
                    upload_token=issue_token(project_id, "upload", paper.id),
                    title=paper.metadata.title,
                    year=paper.metadata.year,
                    reason=acquisitions[paper.id].error,
                )
                for paper in records
                if paper.id in acquisitions
                and acquisitions[paper.id].status == "awaiting_upload"
            ])
        if stage == "failed":
            return RetryAction(reason=detail)
        return WaitAction(job_id=None)

    async def action(self, project_id: str, action: WorkspaceActionRequest) -> WorkspaceMutationResult:
        message, job = "操作已完成", None
        if action.type == "run":
            job_type = "document_analysis" if await self.sessions.selection(project_id) else "research"
            # A fresh run is a deliberate decision to spend more: re-baseline the
            # budget window so the gate asks again only after another block.
            await self.research.ack_budget_gate(project_id)
            await self.hitl.resolve_all(
                project_id, resolved_by=f"action:{action.type}",
                resolution={"action": action.type},
            )
            job = await self.jobs.enqueue(project_id, job_type)
            self.app.state.workflow_worker.wake()
            message = "研究任务已提交"
        elif action.type == "regenerate_search":
            await self.app.state.workflow_worker.cancel_project(project_id)
            instruction = action.instruction.strip() if action.instruction else None
            await self.sessions.begin_search(project_id, instruction)
            await self.projects.reopen(project_id, "searching")
            await self.projects.set_analysis_review_decision(project_id, None)
            # A new search makes any pending selection wait obsolete.
            await self.hitl.resolve_all(
                project_id, status="superseded",
                resolved_by="action:regenerate_search",
                resolution={"action": "regenerate_search"},
            )
            job = await self.jobs.enqueue(project_id, "research")
            self.app.state.workflow_worker.wake()
            message = "已根据补充要求重新提交检索"
        elif action.type == "select_papers":
            current = await self.sessions.current_search(project_id)
            if current is None or current["plan"] is None:
                raise ProjectConflictError("Search has not completed")
            decoded = [
                read_token(token, project_id, "paper")["i"] for token in action.paper_tokens
            ]
            revisions_and_ids = [value.split(":", 1) for value in decoded]
            if any(
                int(revision) != current["revision"] for revision, _ in revisions_and_ids
            ):
                raise ProjectConflictError("Paper token belongs to an outdated search")
            await self.app.state.workflow_worker.cancel_project(project_id)
            await self.sessions.select(
                project_id,
                current["revision"],
                [paper_id for _, paper_id in revisions_and_ids],
                action.analysis_requirements.strip() if action.analysis_requirements else None,
            )
            # A re-selection after a paused / completed run must not inherit the
            # previous selection's per-paper analyses, report or cached section
            # work items: the new paper set is a fresh, isolated analysis run and
            # the UI must render only it (no leftover report/board/counters from
            # the earlier papers). Documents / figures / evidence stay (they are
            # per-paper artifacts reused via cache).
            await self.sessions.reset_analysis(project_id, current["revision"])
            # The human decision consumes the open paper_selection event; only
            # then may the analysis job run (worker guard checks open events).
            await self.hitl.resolve_all(
                project_id, resolved_by="action:select_papers",
                resolution={"action": "select_papers"},
            )
            # A new selection invalidates any earlier review-gate decision.
            await self.projects.set_analysis_review_decision(project_id, None)
            await self.projects.reopen(project_id, "acquiring_selected")
            await self.research.ack_budget_gate(project_id)
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = "已确认论文，正在获取全文并分析"
        elif action.type == "reanalyze_selected":
            current = await self.sessions.current_search(project_id)
            selection = await self.sessions.selection(project_id)
            if current is None or selection is None:
                raise ProjectConflictError("No selected papers are available for reanalysis")
            await self.sessions.reset_analysis(project_id, current["revision"])
            # A regeneration restarts the per-paper runs, so the review gate
            # must re-evaluate (no stale 'continue'/'regenerate' decision).
            await self.projects.set_analysis_review_decision(project_id, None)
            await self.projects.reopen(project_id, "analyzing_selected")
            await self.research.ack_budget_gate(project_id)
            await self.hitl.resolve_all(
                project_id, resolved_by="action:reanalyze_selected",
                resolution={"action": "reanalyze_selected"},
            )
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = "正在基于已有全文和证据重新生成详细分析"
        elif action.type == "evidence_review":
            evidence_id = read_token(action.evidence_token, project_id, "evidence")["i"]
            await self.research.review_evidence(
                project_id, evidence_id, action.status, action.note, source="human"
            )
            message = "证据复核状态已保存"
        elif action.type == "pause_analysis":
            current = await self.projects.get(project_id)
            if current.status != "running":
                message = "当前没有正在运行的分析任务"
            else:
                await self.projects.set_pause_requested(project_id, True)
                message = "已请求在安全边界暂停（当前图表或分析块结束后停下，已完成结果保留）"
        elif action.type == "resume_analysis":
            current = await self.projects.get(project_id)
            if await self.sessions.selection(project_id) is None:
                raise ProjectConflictError("尚未选择论文，无法继续分析")
            if current.current_stage not in {"analysis_paused", "analyzing_selected",
                                             "acquiring_selected"}:
                raise ProjectConflictError("该项目没有处于暂停状态的分析任务")
            await self.projects.set_pause_requested(project_id, False)
            await self.projects.reopen(project_id, "analyzing_selected")
            # "Continue" closes the open budget gate and re-baselines the window
            # (M3): the resumed run only pauses again after another threshold-sized
            # block of spend, and completed units stay cached either way.
            await self.research.ack_budget_gate(project_id)
            await self.hitl.resolve_all(
                project_id, resolved_by="action:resume_analysis",
                resolution={"action": "resume_analysis"},
            )
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = "已从暂停边界继续分析（已完成的部分不会重复计算）"
        elif action.type in {"review_gate_continue", "review_gate_regenerate"}:
            # M3 ReviewGate resolution: the human answered the pre-synthesis
            # evidence-review wait. Write the decision as business-table truth
            # (the graph gate reads it on re-entry) and resume the run.
            decision = (
                "continue" if action.type == "review_gate_continue"
                else "regenerate_after_review"
            )
            current = await self.projects.get(project_id)
            if current.status != "waiting" or current.current_stage != "analysis_paused":
                raise ProjectConflictError("该项目没有处于证据复核门的暂停任务")
            await self.hitl.resolve_all(
                project_id, resolved_by=f"action:{action.type}",
                resolution={"action": action.type, "decision": decision},
            )
            await self.projects.set_analysis_review_decision(project_id, decision)
            await self.projects.reopen(project_id, "analyzing_selected")
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = (
                "已放行：直接生成综合分析报告"
                if decision == "continue"
                else "已提交：将先处理争议证据，再重新生成分析报告"
            )
        elif action.type == "reanalyze_part":
            current = await self.sessions.current_search(project_id)
            selection = await self.sessions.selection(project_id)
            if current is None or current["plan"] is None or selection is None:
                raise ProjectConflictError("需要一次已完成的分析才能局部重析")
            revision = current["revision"]
            revision_text, paper_id = read_token(
                action.paper_token, project_id, "paper"
            )["i"].split(":", 1)
            if int(revision_text) != revision or paper_id not in selection["paper_ids"]:
                raise ProjectConflictError("论文不属于当前选择，无法局部重析")
            if action.instruction:
                await self.sessions.save_part_instruction(
                    project_id, revision, paper_id, action.part, action.instruction
                )
            deleted = await self.items.delete_part_items(
                project_id, revision, paper_id, [action.part]
            )
            await self.sessions.delete_analysis(project_id, revision, paper_id)
            await self.research.set_progress(
                project_id, paper_id, action.part, "queued",
            )
            await self.research.set_progress(
                project_id, "", "synthesis", "queued",
            )
            await self.projects.set_pause_requested(project_id, False)
            await self.projects.reopen(project_id, "analyzing_selected")
            await self.research.ack_budget_gate(project_id)
            await self.hitl.resolve_all(
                project_id, resolved_by="action:reanalyze_part",
                resolution={"action": "reanalyze_part", "part": action.part},
            )
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            hint = f"、已清除 {deleted} 条缓存" if deleted else ""
            message = (
                f"已提交局部重析：重跑该部分{hint}，其余部分复用缓存，报告将重新生成"
            )
        return WorkspaceMutationResult(
            message=message,
            workspace=await self.workspace(project_id),
            job=job,
        )

    async def _desk_scope(self, project_id: str):
        """Citation index + paper ids for the decision desk.

        Prefers the synthesis report; falls back to the persisted per-paper
        analyses at the pre-synthesis review gate, so the human can adjudicate
        the disputed evidence *before* paying for the final synthesis call.
        """
        search = await self.sessions.current_search(project_id)
        if search is None:
            raise RecordNotFoundError("该项目还没有检索记录")
        revision = search["revision"]
        report = await self.sessions.report(project_id, revision)
        if report is not None:
            return citation_index(report), [paper.paper_id for paper in report.papers]
        analyses = await self.sessions.paper_analyses(project_id, revision)
        if not analyses:
            raise ProjectConflictError("分析报告尚未生成，且没有已完成的逐篇分析，暂无证据可复核")
        return citation_index_papers(analyses), [analysis.paper_id for analysis in analyses]

    @staticmethod
    def _claim_refs(claims) -> list[DeskClaimRef]:
        return [
            DeskClaimRef(
                scope=claim.scope,
                paper_title=claim.paper_title,
                section_label=claim.section_label,
                value=claim.value,
                evidence_count=claim.evidence_count,
                will_downgrade=claim.will_downgrade,
            )
            for claim in claims
        ]

    def _desk_item(self, project_id: str, row) -> ReviewDeskItem:
        node = row.evidence
        return ReviewDeskItem(
            evidence_token=issue_token(project_id, "evidence", node.evidence_id),
            resource_url=self._resource_url(project_id, "evidence", node.evidence_id),
            evidence_id=node.evidence_id,
            type=node.evidence_type,
            label=node.label,
            page=node.page_number,
            claim=node.claim,
            excerpt=node.excerpt,
            confidence=node.confidence,
            review_status=row.review.status,
            review_source=row.review.source,
            review_note=row.review.note,
            cited_by=row.cited_by,
            supports_comparison=row.supports_comparison,
            priority=round(row.priority, 4),
            citing=self._claim_refs(row.citing),
        )

    async def review_desk(self, project_id: str, segment: str = "priority") -> ReviewDesk:
        index, paper_ids = await self._desk_scope(project_id)
        nodes: list = []
        for paper_id in paper_ids:
            nodes.extend(await self.evidence.list_for_paper(
                project_id, paper_id, limit=2_000
            ))
        result = build_queue(nodes, await self.research.list_reviews(project_id), index)
        items = [
            self._desk_item(project_id, row)
            for row in filter_rows(result.rows, segment)
        ]
        return ReviewDesk(
            project_id=project_id,
            segment=segment,
            stats=ReviewDeskStats(**result.stats),
            items=items,
        )

    async def review_preview(
        self, project_id: str, body: ReviewPreviewRequest
    ) -> ReviewPreviewResult:
        evidence_id = read_token(body.evidence_token, project_id, "evidence")["i"]
        index, _ = await self._desk_scope(project_id)
        impact = impact_preview(index, evidence_id)
        if body.status == "excluded":
            message = (
                f"这条证据被 {impact.cited_total} 条结论引用；排除后 "
                f"{impact.downgrade_count} 条将失去唯一来源、在重新生成时降级为推断，"
                f"{impact.retained_count} 条仍保留证据支持"
                + ("（含双篇比较结论）" if impact.supports_comparison else "")
                + "。"
            )
            return ReviewPreviewResult(
                evidence_token=body.evidence_token, status=body.status,
                cited_total=impact.cited_total,
                supports_comparison=impact.supports_comparison,
                downgrade=self._claim_refs(impact.downgrade),
                retained=self._claim_refs(impact.retained),
                message=message,
            )
        label = {"confirmed": "确认", "doubted": "存疑", "excluded": "排除"}[body.status]
        if impact.cited_total:
            message = (
                f"该证据支撑 {impact.cited_total} 条结论；{label}后重新生成报告时，"
                "这些结论的证据状态保持不变。"
            )
        else:
            message = "该证据未被任何结论引用，本次复核仅影响其后续使用。"
        return ReviewPreviewResult(
            evidence_token=body.evidence_token, status=body.status,
            cited_total=impact.cited_total,
            supports_comparison=impact.supports_comparison,
            message=message,
        )

    @staticmethod
    def _budget_hint(job, analysis_metrics, usage: dict[str, Any] | None = None,
                     budget_win: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Display-level budget line + M3 cost-gate summary.

        Fields keep the advisory visual/minute hints (``warning_*``/``warned``).
        ``tokens_total`` / ``vision_calls_total`` are reported *per run window*
        (usage since the last re-baseline on continue / re-select / re-analyze),
        not cumulative project spend, so a freshly re-selected paper shows a
        congruent "started ~0 minutes ago, spent ~0 tokens" line instead of the
        previous paper's leftover counters. The gate thresholds (``gate_*``)
        explain a ``budget_gate`` pause.
        """
        settings = get_settings()
        usage = usage or {}
        budget_win = budget_win or {}
        # A window starts either at the human "continue"/re-select (since) or at
        # the job's creation; either way elapsed reflects the current run, not
        # the whole project.
        start = budget_win.get("since") or (job.created_at if job else None)
        try:
            if start is not None:
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                elapsed_minutes = max(
                    0, int((datetime.now(UTC) - start).total_seconds() // 60)
                )
            else:
                elapsed_minutes = 0
        except Exception:  # noqa: BLE001 - hint is cosmetic
            elapsed_minutes = 0
        total = int(analysis_metrics["total_visuals"] or 0)
        completed = int(analysis_metrics["completed_visuals"] or 0)
        warned = (
            total >= settings.budget_warning_vision_calls
            or elapsed_minutes >= settings.budget_warning_minutes
        )
        baseline_tokens = int(budget_win.get("baseline_tokens") or 0)
        baseline_vision = int(budget_win.get("baseline_vision") or 0)
        return {
            "elapsed_minutes": elapsed_minutes,
            "total_visuals": total,
            "completed_visuals": completed,
            "remaining_visuals": max(0, total - completed),
            "warning_vision_calls": settings.budget_warning_vision_calls,
            "warning_minutes": settings.budget_warning_minutes,
            "warned": warned,
            "tokens_total": max(0, int(usage.get("tokens") or 0) - baseline_tokens),
            "vision_calls_total": max(
                0, int(usage.get("vision_calls") or 0) - baseline_vision
            ),
            "gate_enabled": settings.budget_gate_enabled,
            "gate_open": bool(budget_win.get("open")),
            "gate_tokens": settings.budget_gate_tokens,
            "gate_vision_calls": settings.budget_gate_vision_calls,
            "gate_minutes": settings.budget_gate_minutes,
        }

    @property
    def max_upload_bytes(self) -> int:
        return self.document_service.workspace.max_document_bytes

    async def upload(
        self, project_id: str, upload_token: str, filename: str, content: bytes
    ) -> WorkspaceMutationResult:
        paper_id = read_token(upload_token, project_id, "upload")["i"]
        entry = self.document_service.workspace.import_pdf_bytes(
            project_id, filename or "paper.pdf", content
        )
        linked = await self.documents.register(project_id, paper_id, entry)
        await self.transfers.upsert_acquisition(PaperAcquisition(
            project_id=project_id,
            paper_id=paper_id,
            status="parsed",
            document_id=linked.document_id,
            updated_at=utc_now(),
        ))
        # The upload is the human answer to the document_unavailable wait; if
        # other papers are still missing, the next run re-opens the event.
        await self.hitl.resolve_all(
            project_id, resolved_by="action:upload",
            resolution={"action": "upload", "paper_id": paper_id},
        )
        job = await self.jobs.enqueue(project_id, "document_analysis")
        self.app.state.workflow_worker.wake()
        return WorkspaceMutationResult(
            message="PDF 已上传，正在继续分析",
            workspace=await self.workspace(project_id),
            job=job,
        )

    async def resource(self, project_id: str, token: str) -> WorkspaceResource:
        payload = read_token(token, project_id)
        if payload["k"] == "evidence":
            node = await self.evidence.get(project_id, payload["i"])
            preview = EvidenceVerifier(self.document_service).verify(node)
            if preview.content_type == "text":
                return WorkspaceResource(
                    (preview.excerpt or "").encode(), "text/plain; charset=utf-8"
                )
            path = self.document_service.workspace.resolve_safe_path(
                project_id, preview.source_path
            )
            return WorkspaceResource(path.read_bytes(), "image/png")
        if payload["k"] == "visual":
            region = await self.research.get_visual_region(project_id, payload["i"])
            path = self.document_service.workspace.resolve_safe_path(
                project_id, region["source_path"]
            )
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != region["source_hash"]:
                raise DocumentValidationError("Resource hash mismatch")
            return WorkspaceResource(content, "image/png")
        if payload["k"] == "document":
            linked = await self.documents.get(project_id, payload["i"])
            path = self.document_service.workspace.resolve_safe_path(
                project_id, linked.relative_path
            )
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != linked.sha256:
                raise DocumentValidationError("Document hash mismatch")
            return WorkspaceResource(content, "application/pdf")
        raise RecordNotFoundError("资源不存在")
