from time import perf_counter
from uuid import uuid4

from app.agents import (
    Coordinator,
    EvidenceSynthesizer,
    LiteratureResearcher,
    PaperAnalyst,
)
from app.db import (
    DocumentRepository,
    EvidenceRepository,
    PaperRepository,
    ProjectRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    TraceRepository,
    TransferRepository,
    WorkItemRepository,
)
from app.db.repositories import utc_now
from app.documents.acquisition import OpenAccessDownloader
from app.literature import LiteratureToolClient
from app.llm import OllamaProvider
from app.schemas import AgentTask, PaperAcquisition, RankedPaper


async def _resolve_open_access_url(literature, metadata) -> tuple[str | None, str | None]:
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


class ResearchWorkflowService:
    """Run durable paper-research jobs without depending on the HTTP layer."""

    def __init__(self, app, literature: LiteratureToolClient) -> None:
        self.app = app
        self.literature = literature

    async def execute_job(self, job) -> None:
        database = self.app.state.database
        projects = ProjectRepository(database)
        project = await projects.get(job.project_id)
        resume = project.status in {"waiting", "failed"}
        async with OllamaProvider() as provider:
            if job.job_type == "document_analysis":
                await self._analyze_selected(job, provider, projects)
                return
            await self._run_research(job, provider, projects, resume=resume)

    async def _analyze_selected(self, job, provider, projects: ProjectRepository) -> None:
        database = self.app.state.database
        sessions = ResearchSessionRepository(database)
        selection = await sessions.selection(job.project_id)
        if not selection:
            raise RuntimeError("No selected papers are ready for analysis")
        project = await projects.get(job.project_id)
        await projects.start_run(
            job.project_id, job.run_id, resume=project.status in {"waiting", "failed"}
        )
        papers = PaperRepository(database)
        documents = DocumentRepository(database)
        evidence = EvidenceRepository(database)
        research = ResearchDataRepository(database)
        transfer = TransferRepository(database)
        traces = TraceRepository(database)
        document_service = self.app.state.document_service
        acquisitions = {item.paper_id: item for item in await transfer.list_acquisitions(job.project_id)}
        missing = False
        for paper_id in selection["paper_ids"]:
            paper = await papers.get(job.project_id, paper_id)
            acquisition = acquisitions.get(paper_id)
            if acquisition and acquisition.status == "parsed":
                continue
            source_url, discovery_error = await _resolve_open_access_url(
                self.literature, paper.metadata
            )
            acquisition = PaperAcquisition(
                project_id=job.project_id, paper_id=paper_id, status="awaiting_upload",
                source_url=source_url, error=discovery_error, updated_at=utc_now(),
            )
            if source_url:
                try:
                    await transfer.upsert_acquisition(
                        acquisition.model_copy(update={"status": "downloading"})
                    )
                    content, final_url = await OpenAccessDownloader(
                        document_service.workspace.max_document_bytes
                    ).fetch(source_url)
                    entry = document_service.workspace.import_pdf_bytes(
                        job.project_id, f"{paper_id}.pdf", content
                    )
                    await self.app.state.document_capabilities.parse_document(
                        job.project_id, entry.document_id, job.run_id
                    )
                    linked = await documents.register(job.project_id, paper_id, entry)
                    acquisition = acquisition.model_copy(update={
                        "status": "parsed", "source_url": final_url,
                        "document_id": linked.document_id, "updated_at": utc_now(),
                    })
                except Exception as exc:  # noqa: BLE001 - upload is the explicit fallback
                    acquisition = acquisition.model_copy(update={
                        "status": "awaiting_upload", "error": str(exc)[:1_000],
                        "updated_at": utc_now(),
                    })
            await transfer.upsert_acquisition(acquisition)
            missing = missing or acquisition.status != "parsed"
        if missing:
            await projects.wait(job.project_id, "awaiting_documents")
            return

        await projects.set_stage(job.project_id, "analyzing_selected")
        analyst = PaperAnalyst(
            provider,
            document_service,
            documents,
            evidence,
            research,
            WorkItemRepository(database),
            self.app.state.skill_registry,
            traces,
        )
        analyses = await sessions.paper_analyses(job.project_id, selection["search_revision"])
        completed = {item.paper_id for item in analyses}
        for paper_id in selection["paper_ids"]:
            if paper_id in completed:
                continue
            paper = await papers.get(job.project_id, paper_id)
            linked = await documents.get_for_paper(job.project_id, paper_id)
            await self.app.state.document_capabilities.parse_document(
                job.project_id, linked.document_id, job.run_id
            )
            analysis = await analyst.run(
                job.project_id,
                selection["search_revision"],
                paper,
                linked,
                project.goal,
                selection["requirements"],
                trace_id=f"job-{job.run_id}",
            )
            await sessions.save_paper_analysis(
                job.project_id, selection["search_revision"], analysis
            )
            analyses.append(analysis)
        await EvidenceSynthesizer(provider, sessions).run(
            job.project_id, selection["search_revision"], project.goal,
            selection["requirements"], analyses,
        )
        await projects.complete(job.project_id, "analysis_review")

    async def _analyze_pending_documents(self, project_id: str, provider) -> None:
        from app.documents.analysis import DocumentAnalysisPipeline

        database = self.app.state.database
        research = ResearchDataRepository(database)
        pipeline = DocumentAnalysisPipeline(
            self.app.state.document_service,
            DocumentRepository(database),
            EvidenceRepository(database),
            research,
            provider,
        )
        for document in await research.pending_documents(project_id):
            await pipeline.run(
                project_id, document["paper_id"], document["id"], document["sha256"]
            )

    async def _run_research(self, job, provider, projects, *, resume: bool) -> None:
        database = self.app.state.database
        papers = PaperRepository(database)
        traces = TraceRepository(database)
        items = WorkItemRepository(database)
        sessions = ResearchSessionRepository(database)
        search_session = await sessions.pending_search(job.project_id)
        trace_id = f"job-{job.job_id}-{uuid4()}"
        project, claimed = await projects.start_run(job.project_id, job.run_id, resume=resume)
        if not claimed:
            return

        started = perf_counter()
        await traces.append(
            job.project_id,
            trace_id,
            "research_started" if not resume else "research_resumed",
            success=True,
            agent="research_workflow",
            summary={"resume": resume},
        )
        researcher = LiteratureResearcher(
            provider,
            self.literature,
            self.app.state.checkpointer,
            self.app.state.skill_registry,
            traces,
            items,
        )
        coordinator = Coordinator(researcher, traces)
        task = AgentTask(
            task_id=job.run_id,
            project_id=job.project_id,
            task_type="literature_search",
            objective=project.goal,
            context={
                "request": project.request.model_copy(update={
                    "constraints": [
                        *project.request.constraints,
                        *([search_session["instruction"]] if search_session["instruction"] else []),
                    ]
                }).model_dump(mode="json"),
                "resume": False,
                "search_revision": search_session["revision"],
                "profile": None,
            },
        )
        try:
            result = (await coordinator.run(task, trace_id)).output
            ranked = [RankedPaper.model_validate(item) for item in result["ranked_papers"]]
            stored = [await papers.upsert_ranked(job.project_id, item) for item in ranked]
            plan = {
                "understanding": result.get("understanding"),
                "strategy": result.get("plan", {}).get("search_strategy", {}),
                "queries": result.get("search_queries", []),
                "sources": project.request.literature_sources,
                "year_from": project.request.year_from,
                "year_to": project.request.year_to,
                "warnings": result.get("warnings", []),
                "instruction": search_session.get("instruction"),
            }
            await sessions.complete_search(
                job.project_id, search_session["revision"], plan, [item.id for item in stored]
            )
            completed = await projects.wait(job.project_id, "paper_selection")
            await traces.append(
                job.project_id,
                trace_id,
                "research_completed" if completed.status == "completed" else "workflow_waiting",
                success=True,
                agent="research_workflow",
                latency_ms=round((perf_counter() - started) * 1000),
                summary={
                    "selected_paper_count": 0,
                    "current_stage": completed.current_stage,
                },
            )
        except Exception as exc:
            await projects.fail(
                job.project_id,
                "failed",
                {"type": type(exc).__name__, "message": str(exc)[:1_000]},
            )
            await traces.append(
                job.project_id,
                trace_id,
                "research_failed",
                success=False,
                agent="research_workflow",
                latency_ms=round((perf_counter() - started) * 1000),
                error={"type": type(exc).__name__, "message": str(exc)[:1_000]},
            )
            raise

