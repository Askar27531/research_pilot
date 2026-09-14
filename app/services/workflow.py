from time import perf_counter
from uuid import uuid4

from app.agents import LiteratureResearcher
from app.db import (
    DocumentRepository,
    EvidenceRepository,
    HitlEventRepository,
    PaperRepository,
    ProjectRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    TraceRepository,
    TransferRepository,
    WorkflowJobRepository,
    WorkItemRepository,
)
from app.graph.analysis_graph import build_analysis_graph
from app.graph.analysis_nodes import AnalysisGraphContext, AnalysisJobStop
from app.literature import LiteratureToolClient
from app.llm import OllamaProvider
from app.reliability.faults import AnalysisPausedError
from app.schemas import AgentTask, RankedPaper


class ResearchWorkflowService:
    """Run durable paper-research jobs without depending on the HTTP layer.

    Two LangGraph checkpointer-backed graphs share one ``AsyncSqliteSaver``:
      - graph ① search_graph  (literature search; thread ``…:search-…:…``)
      - graph ② analysis_graph (document analysis; thread ``…:analysis:…``)
    Human waits are expressed as ``hitl_events`` (waiting_for_human); graph
    nodes park the run and a resolving action enqueues a resume job.
    """

    def __init__(self, app, literature: LiteratureToolClient) -> None:
        self.app = app
        self.literature = literature

    # Durable-Execution: 任务级执行入口：worker 守卫（存在 open hitl_event 时任何唤醒都被拒绝，人工裁决是唯一出路）→ 判断 resume(项目 waiting/failed) → provider 挂逐调用 usage 计量；分析 job 捕获 AnalysisPausedError 在幂等安全边界落定（人工/预算暂停，续跑零重算）。
    async def execute_job(self, job) -> None:
        database = self.app.state.database
        projects = ProjectRepository(database)
        project = await projects.get(job.project_id)      # 读项目（课题/状态）——"课题复活"
        resume = project.status in {"waiting", "failed"}  # 是续跑还是新跑？
        research = ResearchDataRepository(database)

        # Worker guard (event-ized HITL): while a project holds an open
        # hitl_event (waiting_for_human), no queued/wake-up job may run — a
        # human decision must resolve the event first (the resolving action
        # enqueues the follow-up job itself). Auto-retries can never bypass it.
        hitl = HitlEventRepository(database)
        if await hitl.has_open(job.project_id):            # 有没有"等人"的未决事件？
            open_events = await hitl.open_events(job.project_id)
            await TraceRepository(database).append(        # 记一条审计日志
                job.project_id,
                f"gate-{job.run_id}",
                "hitl_gate_skipped",
                success=True,
                agent="workflow_worker",
                summary={
                    "event_types": [event["type"] for event in open_events],
                    "message": "open hitl_event exists; awaiting a human decision",
                },
            )
            return                                         # ← 直接返回，不跑任务（等人裁决）

        async def _record_usage(usage) -> None:            # 记账回调：每次 LLM 调用记 token（喂预算门）
            # Meter every successful provider call of this job into the run's
            # append-only usage ledger (M3 cost-threshold pause).
            await research.record_usage(
                job.project_id, phase=job.job_type, kind=usage.kind,
                model=usage.model, prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens, latency_ms=usage.latency_ms,
            )

        async with OllamaProvider(usage_sink=_record_usage) as provider:  # 建 LLM 客户端（挂记账回调）
            if job.job_type == "document_analysis":         # 按任务类型分流
                try:
                    await self._run_analysis_graph(         # 分析图（读全文/图表/逐篇分析）
                        job, provider, projects, resume=resume
                    )
                except AnalysisPausedError as exc:
                    # Cooperative pause (manual or cost gate): park the project at
                    # a safe boundary and let the job finish without a failure.
                    # Resume reuses every persisted unit (documents/visual
                    # regions/work items). A budget pause is also recorded in
                    # projects.pause_reason and as an open budget window so the
                    # UI can tell it apart and re-baseline on "continue".
                    reason = exc.reason or "user"
                    await research.park_running_stages(job.project_id)
                    await projects.reopen(job.project_id, "analysis_paused")
                    await projects.set_pause_reason(job.project_id, reason)
                    traces = TraceRepository(database)
                    await traces.append(
                        job.project_id,
                        f"pause-{job.run_id}",
                        "analysis_paused",
                        success=True,
                        agent="research_workflow",
                        summary={"message": str(exc), "pause_reason": reason},
                    )
                return
            await self._run_research(job, provider, projects, resume=resume)  # ← 检索图（本次例子走这条）

    # Durable-Execution: 分析图驱动：start_run 认领 run 后以原 run_id 拼 thread_id 重入图，LangGraph checkpoint 保证已完成超步不重放、interrupt 冻结点可续跑；门节点以业务表为真源放行。
    async def _run_analysis_graph(
        self, job, provider, projects: ProjectRepository, *, resume: bool
    ) -> None:
        """Drive the analysis LangGraph (graph ②) for one document_analysis job.

        Run claim (start_run) lives here, before graph invocation, so resume /
        parking semantics match the pre-graph worker. Business idempotency
        (work_items.input_hash / visual_regions keys / persisted paper analyses)
        makes every re-entry of the graph replay-safe.
        """
        database = self.app.state.database
        _, claimed = await projects.start_run(
            job.project_id, job.run_id, resume=resume
        )
        if not claimed:
            return
        ctx = AnalysisGraphContext(
            database=database,
            project_id=job.project_id,
            run_id=job.run_id,
            provider=provider,
            projects=projects,
            research=ResearchDataRepository(database),
            sessions=ResearchSessionRepository(database),
            papers=PaperRepository(database),
            documents=DocumentRepository(database),
            evidence=EvidenceRepository(database),
            transfer=TransferRepository(database),
            traces=TraceRepository(database),
            items=WorkItemRepository(database),
            document_service=self.app.state.document_service,
            capabilities=self.app.state.document_capabilities,
            skill_registry=self.app.state.skill_registry,
            literature=self.literature,
            checkpointer=self.app.state.checkpointer,
        )
        graph = build_analysis_graph(ctx)
        thread_id = f"{job.project_id}:analysis:{job.run_id}"
        try:
            result = await graph.ainvoke(
                {"project_id": job.project_id},
                config={"configurable": {"thread_id": thread_id}},
            )
        except AnalysisJobStop:
            # Graceful park (e.g. document_unavailable): the waiting event is
            # already recorded by the node; finish the job as a normal success.
            return
        if result.get("__interrupt__"):
            # M2 SelectGate (and later M3 ReviewGate): a gate node parked the
            # project (waiting + open hitl_event) and froze the graph on this
            # thread. The resolving action re-enters with a fresh job whose
            # gate reads the decision from the business tables.
            await TraceRepository(database).append(
                job.project_id,
                f"graph-{job.run_id}",
                "graph_interrupt",
                success=True,
                agent="research_workflow",
                summary={
                    "thread_id": thread_id,
                    "current_stage": (await projects.get(job.project_id)).current_stage,
                    "interrupts": len(result["__interrupt__"]),
                },
            )
            return

    # Durable-Execution: 检索图驱动：以项目:search-{revision}:{track} 作为 run 档位；全部外部检索调用走 work-item 参数指纹缓存、结果按 stable_key upsert——失败重试只补未完成部分，不重付外部请求。
    async def _run_research(self, job, provider, projects, *, resume: bool) -> None:
        database = self.app.state.database
        papers = PaperRepository(database)
        traces = TraceRepository(database)
        items = WorkItemRepository(database)
        sessions = ResearchSessionRepository(database)
        search_session = await sessions.pending_search(job.project_id)   # 读检索会话（revision + instruction）
        trace_id = f"job-{job.job_id}-{uuid4()}"
        project, claimed = await projects.start_run(job.project_id, job.run_id, resume=resume)  # ← 原子认领项目 run（created→running）
        if not claimed:
            return                                        # 已被领过 → 幂等返回，不重跑

        started = perf_counter()                           # 计时开始
        await traces.append(                               # 记一条"检索开始"日志
            job.project_id,
            trace_id,
            "research_started" if not resume else "research_resumed",
            success=True,
            agent="research_workflow",
            summary={"resume": resume},
        )
        researcher = LiteratureResearcher(                 # 建检索 Agent（包 LLM + 文献客户端 + checkpointer）
            provider,
            self.literature,
            self.app.state.checkpointer,
            self.app.state.skill_registry,
            traces,
            items,
        )
        task = AgentTask(                                  # 把课题打包成任务输入
            task_id=job.run_id,
            project_id=job.project_id,
            task_type="literature_search",
            objective=project.goal,                        # 课题字符串
            context={
                "request": project.request.model_copy(update={
                    "constraints": [
                        *project.request.constraints,
                        *([search_session["instruction"]] if search_session["instruction"] else []),  # 附加"重新检索"的补充要求
                    ]
                }).model_dump(mode="json"),                # 完整课题 JSON（真正喂 LLM 的）
                "resume": False,
                "search_revision": search_session["revision"],   # 版本号 → 拼 checkpoint 档位
            },
        )
        try:
            result = (await researcher.run(task, trace_id)).output  # ← 跑 8 节点检索图（核心长活）
            ranked = [RankedPaper.model_validate(item) for item in result["ranked_papers"]]
            stored = [await papers.upsert_ranked(job.project_id, item) for item in ranked]  # 论文落库
            plan = {                                        # 组"检索计划"（存进 search_sessions.plan_json 供 UI 回显）
                "understanding": result.get("understanding"),
                "strategy": result.get("plan", {}).get("search_strategy", {}),
                "queries": result.get("search_queries", []),   # ← LLM 生成的检索词
                "sources": project.request.literature_sources,
                "year_from": project.request.year_from,
                "year_to": project.request.year_to,
                "warnings": result.get("warnings", []),
                "instruction": search_session.get("instruction"),
            }
            await sessions.complete_search(                # 写 plan_json + 关联命中的论文
                job.project_id, search_session["revision"], plan, [item.id for item in stored]
            )
            # M2: the analysis graph owns the paper-selection wait now. Park the
            # project at the selection stage and hand over to a document_analysis
            # job — its select_gate node freezes via interrupt() and opens the
            # paper_selection hitl_event (no LLM spend while waiting).
            await projects.reopen(job.project_id, "paper_selection")   # 项目停到"选文"（等人）
            follow_up = await WorkflowJobRepository(database).enqueue(
                job.project_id, "document_analysis"          # 交棒：排下一条"分析"任务
            )
            self.app.state.workflow_worker.wake()            # ← 叫醒 worker 去领新任务
            await traces.append(                           # 记一条"检索完成"日志（含总耗时）
                job.project_id,
                trace_id,
                "research_completed",
                success=True,
                agent="research_workflow",
                latency_ms=round((perf_counter() - started) * 1000),
                summary={
                    "selected_paper_count": 0,
                    "current_stage": "paper_selection",
                    "follow_up_job": follow_up.job_id,
                    "pending_selection": True,
                },
            )
        except Exception as exc:                             # 失败兜底
            await projects.fail(                             # 项目标失败 + 错误快照
                job.project_id,
                "failed",
                {"type": type(exc).__name__, "message": str(exc)[:1_000]},
            )
            await traces.append(                           # 记一条"检索失败"日志
                job.project_id,
                trace_id,
                "research_failed",
                success=False,
                agent="research_workflow",
                latency_ms=round((perf_counter() - started) * 1000),
                error={"type": type(exc).__name__, "message": str(exc)[:1_000]},
            )
            raise                                          # ← 重新抛出，让 _run() 的 except 把任务也标失败
