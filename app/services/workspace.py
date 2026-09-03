import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.types import Command

from app.agents import ResearchBuilder
from app.api.tokens import issue_token, read_token
from app.artifacts import ArtifactService, generate_experiment_artifacts
from app.db import (
    ArtifactRepository,
    DocumentRepository,
    EvidenceRepository,
    PaperRepository,
    ProjectRepository,
    ProposalRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    RevisionPreviewRepository,
    SummaryRepository,
    TraceRepository,
    TransferRepository,
    WorkflowJobRepository,
    WorkItemRepository,
)
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import utc_now
from app.documents import DocumentValidationError
from app.evidence import EvidenceVerifier
from app.experiments import ProposalBuilder, build_experiment_graph
from app.llm import OllamaProvider
from app.schemas import (
    AgentTask,
    ExperimentProposal,
    ExperimentRevisionDraft,
    PaperAcquisition,
    ProjectSummary,
    ProjectWorkspace,
    ProposalDecision,
    ResearchProfileInput,
    ResearchRequest,
    TransferCandidateSet,
    TransferDecision,
    WorkspaceActionRequest,
    WorkspaceMutationResult,
    WorkspaceProjectCreate,
    WorkspaceProjectUpdate,
)
from app.schemas.workspace import (
    DownloadAction,
    MissingDocument,
    RetryAction,
    ReviewDirectionAction,
    ReviewExperimentAction,
    UploadDocumentsAction,
    WaitAction,
    WorkspaceProgress,
)


async def _optional(awaitable):
    try:
        return await awaitable
    except RecordNotFoundError:
        return None


def _analysis_claims(value):
    overview = getattr(value, "overview", None)
    if overview is not None:
        yield overview
    for name in (
        "core_problem", "methods", "mechanisms", "experimental_setup", "main_results",
        "limitations", "relevance_to_topic", "commonalities", "differences",
        "complementarities", "applicability",
    ):
        yield from getattr(value, name, [])


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
        self.proposals = ProposalRepository(database)
        self.artifacts = ArtifactRepository(database)
        self.items = WorkItemRepository(database)
        self.jobs = WorkflowJobRepository(database)
        self.evidence = EvidenceRepository(database)
        self.summaries = SummaryRepository(database)
        self.research = ResearchDataRepository(database)
        self.sessions = ResearchSessionRepository(database)
        self.previews = RevisionPreviewRepository(database)
        self.traces = TraceRepository(database)
        self.documents = DocumentRepository(database)
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
            "transfer_approval": (
                "direction_review", "请审阅研究方向", "已形成证据支持的研究建议。"
            ),
            "experiment_design": (
                "researching", "正在生成研究方案", "正在将方向转化为实验计划。"
            ),
            "experiment_approval": (
                "experiment_review", "请确认研究方案", "计划已准备好，不会执行实验。"
            ),
            "human_approval": (
                "experiment_review", "请确认研究方案", "计划已准备好，不会执行实验。"
            ),
        }
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
        request = ResearchRequest(
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
        profile = ResearchProfileInput(
            problem_statement=body.research_question,
            objectives=[body.research_question],
            baseline=body.current_approach,
            metrics=body.target_metrics,
            pain_points=body.difficulties,
        )
        project_id, job_id = await self.research.create_workspace(
            body.project_name or body.research_question[:120], request, profile
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
        request, profile = self._project_inputs(body)
        await self.projects.update_definition(project_id, body.project_name, request)
        await self.transfers.upsert_profile(project_id, profile)
        return WorkspaceMutationResult(
            message="课题设置已保存，可前往检索文献阶段重新检索",
            workspace=await self.workspace(project_id),
        )

    @staticmethod
    def _project_inputs(body: WorkspaceProjectCreate) -> tuple[ResearchRequest, ResearchProfileInput]:
        request = ResearchRequest(
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
        profile = ResearchProfileInput(
            problem_statement=body.research_question,
            objectives=[body.research_question],
            baseline=body.current_approach,
            metrics=body.target_metrics,
            pain_points=body.difficulties,
        )
        return request, profile

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
        (
            project,
            job,
            metrics,
            direction,
            experiment,
            records,
            acquisition_list,
            profile,
            research_profile,
            analysis_metrics,
            artifact_records,
            review_counts,
        ) = await asyncio.gather(
            self.projects.get(project_id),
            self.jobs.latest_for_project(project_id),
            self.items.metrics(project_id),
            _optional(self.transfers.get_candidates(project_id)),
            _optional(self.proposals.get(project_id)),
            self.papers.list_for_project(project_id),
            self.transfers.list_acquisitions(project_id),
            self.research.analysis_profile(project_id),
            self.transfers.get_profile(project_id),
            self.research.analysis_metrics(project_id),
            self.artifacts.list_for_project(project_id),
            self.research.review_counts(project_id),
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
        resources = [{
            "name": value.name,
            "type": value.artifact_type,
            "version": value.version,
            "url": self._resource_url(project_id, "artifact", value.artifact_id),
        } for value in artifact_records]
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
            ),
            direction=direction,
            experiment=experiment,
            artifacts=[],
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
            evidence_review=review_counts,
            output_freshness="stale" if profile.get("outputs_stale") else "fresh",
            resources=resources,
            diagnostics={
                "project_status": project.status,
                "current_stage": project.current_stage,
                "project_version": project.version,
                "job": job.model_dump(mode="json") if job else None,
                "analysis_mode": profile.get("mode", "legacy"),
            },
            project_input={
                "project_name": project.name,
                "research_question": project.request.research_question,
                "current_approach": research_profile.baseline,
                "difficulties": research_profile.pain_points,
                "target_metrics": research_profile.metrics,
                "advanced": {
                    "year_from": project.request.year_from,
                    "year_to": project.request.year_to,
                    "max_papers": project.request.maximum_papers,
                    "sources": project.request.literature_sources,
                },
            },
        )

    def _paper_card(self, project_id, revision, paper, acquisition) -> dict:
        return {
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
        paper = next((value for value in records if value.id == paper_id), None)
        if paper is None:
            raise RecordNotFoundError("Paper not found")
        summary, nodes = await asyncio.gather(
            _optional(self.summaries.get(project_id, paper_id)),
            self.evidence.list_for_paper(project_id, paper_id),
        )
        acquisition = acquisitions.get(paper_id)
        regions = (
            await self.research.visual_regions_for_document(project_id, acquisition.document_id)
            if acquisition and acquisition.document_id else []
        )
        return {
            "paper_token": paper_token,
            "title": paper.metadata.title,
            "metadata": paper.metadata.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json") if summary else None,
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
        evidence_ids = {
            identifier
            for paper in report.papers
            for claim in _analysis_claims(paper)
            for identifier in claim.evidence_ids
        }
        if report.comparison:
            evidence_ids.update(
                identifier
                for claim in _analysis_claims(report.comparison)
                for identifier in claim.evidence_ids
            )
        nodes = await asyncio.gather(*(
            _optional(self.evidence.get(project_id, identifier)) for identifier in evidence_ids
        ))
        evidence_by_id = {node.evidence_id: node for node in nodes if node is not None}

        def replace(value):
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
                            "resource_url": self._resource_url(
                                project_id, "evidence", identifier
                            ),
                        }
                        for identifier in value.pop("evidence_ids")
                        if identifier in evidence_by_id
                    ]
                for item in value.values():
                    replace(item)
            elif isinstance(value, list):
                for item in value:
                    replace(item)

        replace(payload)
        return payload

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
        if stage == "direction_review":
            return ReviewDirectionAction()
        if stage == "experiment_review":
            return ReviewExperimentAction()
        if stage == "failed":
            return RetryAction(reason=detail)
        if stage == "completed":
            return DownloadAction()
        return WaitAction(job_id=None)

    async def action(self, project_id: str, action: WorkspaceActionRequest) -> WorkspaceMutationResult:
        message, job, preview = "操作已完成", None, None
        if action.type == "run":
            job_type = "document_analysis" if await self.sessions.selection(project_id) else "research"
            job = await self.jobs.enqueue(project_id, job_type)
            self.app.state.workflow_worker.wake()
            message = "研究任务已提交"
        elif action.type == "regenerate_search":
            await self.app.state.workflow_worker.cancel_project(project_id)
            instruction = action.instruction.strip() if action.instruction else None
            await self.sessions.begin_search(project_id, instruction)
            await self.projects.reopen(project_id, "searching")
            job = await self.jobs.enqueue(project_id, "research")
            self.app.state.workflow_worker.wake()
            message = "已根据补充要求重新提交检索"
        elif action.type == "reselect_papers":
            current = await self.sessions.current_search(project_id)
            if current is None or current["status"] != "completed":
                raise ProjectConflictError("No completed search is available for paper selection")
            await self.app.state.workflow_worker.cancel_project(project_id)
            await self.sessions.reset_selection(project_id)
            await self.projects.reopen(project_id, "paper_selection")
            message = "已返回论文选择阶段"
        elif action.type == "select_papers":
            current = await self.sessions.current_search(project_id)
            if current is None or current["status"] != "completed":
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
            await self.projects.reopen(project_id, "acquiring_selected")
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = "已确认论文，正在获取全文并分析"
        elif action.type == "reanalyze_selected":
            current = await self.sessions.current_search(project_id)
            selection = await self.sessions.selection(project_id)
            if current is None or selection is None:
                raise ProjectConflictError("No selected papers are available for reanalysis")
            await self.sessions.reset_analysis(project_id, current["revision"])
            await self.projects.reopen(project_id, "analyzing_selected")
            job = await self.jobs.enqueue(project_id, "document_analysis")
            self.app.state.workflow_worker.wake()
            message = "正在基于已有全文和证据重新生成详细分析"
        elif action.type == "direction_decision":
            await self._decide_direction(project_id, action.decision)
            message = ("已采用研究方向，正在生成研究方案"
                       if action.decision == "accept" else "项目已结束")
        elif action.type == "direction_revision_preview":
            value = await self._preview_direction(project_id, action.instruction)
            preview = self._preview_payload(project_id, value)
            message = "调整预览已生成，确认后才会应用"
        elif action.type == "direction_revision_apply":
            await self._apply_direction(project_id, action.preview_token)
            message = "研究方向调整已应用"
        elif action.type == "plan_decision":
            await self._decide_plan(project_id, action.decision)
            if action.decision == "accept":
                await self.research.mark_outputs_fresh(project_id)
            message = ("研究方案已确认并生成材料"
                       if action.decision == "accept" else "项目已结束")
        elif action.type == "plan_revision_preview":
            value = await self._preview_plan(project_id, action.instruction)
            preview = self._preview_payload(project_id, value)
            message = "方案调整预览已生成，确认后才会应用"
        elif action.type == "plan_revision_apply":
            await self._apply_plan(project_id, action.preview_token)
            await self.research.mark_outputs_fresh(project_id)
            message = "方案调整已应用并生成材料"
        elif action.type == "evidence_review":
            evidence_id = read_token(action.evidence_token, project_id, "evidence")["i"]
            await self.research.review_evidence(
                project_id, evidence_id, action.status, action.note
            )
            message = "证据复核状态已保存"
        elif action.type == "refresh_results":
            job = await self.jobs.enqueue(project_id, "resynthesis")
            self.app.state.workflow_worker.wake()
            message = "正在根据复核结果更新研究材料"
        return WorkspaceMutationResult(
            message=message,
            workspace=await self.workspace(project_id),
            job=job,
            preview=preview,
        )

    async def _decide_direction(self, project_id: str, decision: str) -> None:
        current = await self.transfers.get_candidates(project_id)
        await self.transfers.decide_candidates(
            project_id, TransferDecision(action=decision, revision=current.revision)
        )
        if decision == "reject":
            await self.projects.complete(project_id, "transfer_rejected")
        else:
            await self.projects.wait(project_id, "experiment_design")
            await self.jobs.enqueue(project_id)
            self.app.state.workflow_worker.wake()

    @asynccontextmanager
    async def _provider(self):
        async with OllamaProvider() as provider:
            yield provider

    async def _preview_direction(self, project_id: str, instruction: str):
        current = await self.transfers.get_candidates(project_id)
        async with self._provider() as provider:
            result = await ResearchBuilder(
                provider, self.transfers, self.evidence, self.app.state.skill_registry
            ).run(AgentTask(
                task_id=f"preview:{project_id}",
                project_id=project_id,
                task_type="research_build",
                objective="Preview revised transfer candidates",
                context={"feedback": instruction, "persist": False},
            ))
        if result.status != "completed":
            raise ValueError(result.summary)
        candidate = TransferCandidateSet.model_validate(result.output["transfer_candidates"])
        titles = [value.title for value in candidate.combinations[:3]]
        return await self.previews.create(
            project_id,
            "transfer",
            current.revision,
            instruction,
            "；".join(titles) if titles else "已重新评估研究方向",
            candidate.model_dump(mode="json"),
        )

    async def _apply_direction(self, project_id: str, token: str) -> None:
        preview = await self.previews.get(read_token(token, project_id, "preview")["i"])
        if preview.project_id != project_id or preview.target != "transfer":
            raise RecordNotFoundError("Transfer revision preview not found")
        current = await self.transfers.get_candidates(project_id)
        if preview.status != "pending" or current.revision != preview.base_version:
            raise ProjectConflictError("Transfer candidates changed; create a new preview")
        await self.transfers.save_candidates(TransferCandidateSet.model_validate(preview.patch))
        await self.previews.mark_applied(preview.preview_id)

    async def _decide_plan(self, project_id: str, decision: str, *, updates=None,
                           feedback: str | None = None) -> ExperimentProposal:
        current = await self.proposals.get(project_id)
        value = ProposalDecision(
            action="modify" if updates is not None else decision,
            version=current.version,
            feedback=feedback,
            experiment_updates=updates or [],
        )
        async with self._provider() as provider:
            graph = build_experiment_graph(
                ProposalBuilder(provider, self.evidence, self.app.state.skill_registry),
                self.app.state.checkpointer,
            )
            result = await graph.ainvoke(
                Command(resume=value.model_dump(mode="json")),
                config={"configurable": {"thread_id": f"experiment:{project_id}"}},
            )
        saved = await self.proposals.decide(
            project_id, value, ExperimentProposal.model_validate(result["proposal"])
        )
        if saved.status in {"accepted", "modified"}:
            await generate_experiment_artifacts(
                ArtifactService(self.document_service.workspace, self.artifacts), saved
            )
        await self.projects.complete(project_id, f"proposal_{saved.status}")
        await self.traces.append(
            project_id,
            saved.proposal_id,
            "human_approval_resumed",
            success=True,
            agent="research_planner",
            summary={"action": value.action, "from_version": value.version},
        )
        return saved

    async def _preview_plan(self, project_id: str, instruction: str):
        current = await self.proposals.get(project_id)
        async with self._provider() as provider:
            draft = await provider.structured_output([
                {"role": "system", "content": (
                    "Translate the user's experiment revision into the smallest valid structured "
                    "update. Only reference experiment IDs in the current proposal. "
                    "Do not change unspecified fields."
                )},
                {"role": "user", "content": (
                    f"CURRENT PROPOSAL:\n{current.model_dump_json()}\n"
                    f"USER REQUEST:\n{instruction}"
                )},
            ], ExperimentRevisionDraft)
        known_ids = {item.experiment_id for item in current.experiments}
        if any(item.experiment_id not in known_ids for item in draft.experiment_updates):
            raise ValueError("Experiment revision references an unknown experiment")
        return await self.previews.create(
            project_id,
            "experiment",
            current.version,
            instruction,
            draft.summary,
            {"experiment_updates": [value.model_dump(mode="json")
                                    for value in draft.experiment_updates]},
        )

    async def _apply_plan(self, project_id: str, token: str) -> None:
        preview = await self.previews.get(read_token(token, project_id, "preview")["i"])
        if preview.project_id != project_id or preview.target != "experiment":
            raise RecordNotFoundError("Experiment revision preview not found")
        current = await self.proposals.get(project_id)
        if preview.status != "pending" or current.version != preview.base_version:
            raise ProjectConflictError("Experiment proposal changed; create a new preview")
        await self._decide_plan(
            project_id,
            "modify",
            updates=preview.patch["experiment_updates"],
            feedback=preview.instruction,
        )
        await self.previews.mark_applied(preview.preview_id)

    @staticmethod
    def _preview_payload(project_id, preview) -> dict:
        return {
            "preview_token": issue_token(project_id, "preview", preview.preview_id),
            "summary": preview.summary,
            "patch": preview.patch,
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
        job = await self.jobs.enqueue(project_id, "document_analysis")
        self.app.state.workflow_worker.wake()
        return WorkspaceMutationResult(
            message="PDF 已上传，正在继续分析",
            workspace=await self.workspace(project_id),
            job=job,
        )

    async def resource(self, project_id: str, token: str) -> WorkspaceResource:
        payload = read_token(token, project_id)
        if payload["k"] == "artifact":
            record = await self.artifacts.get(project_id, payload["i"])
            content = await ArtifactService(
                self.document_service.workspace, self.artifacts
            ).read(record)
            media = {"markdown": "text/markdown", "csv": "text/csv",
                     "mermaid": "text/plain"}[record.artifact_type]
            return WorkspaceResource(content, media, record.name)
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
