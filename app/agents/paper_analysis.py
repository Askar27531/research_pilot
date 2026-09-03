import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.db import (
    DocumentRepository,
    EvidenceRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    WorkItemRepository,
)
from app.documents import DocumentService
from app.documents.analysis import DocumentAnalysisPipeline
from app.evidence import EvidenceBuilder
from app.llm import LLMError, LLMProvider
from app.schemas import (
    AnalysisReport,
    ComparisonDraft,
    CriticalAnalysisDraft,
    EvidenceNode,
    ExperimentAnalysisDraft,
    MethodAnalysisDraft,
    PaperAnalysis,
    PaperOverviewDraft,
    ProblemAnalysisDraft,
    TextEvidenceCreate,
)
from app.skills import SkillRegistry

DraftT = TypeVar("DraftT", bound=BaseModel)
ANALYSIS_PIPELINE_VERSION = 3
CHUNK_SIZE = 950
CHUNK_OVERLAP = 140


def _claims(value):
    overview = getattr(value, "overview", None)
    if overview is not None:
        yield overview
    for name in (
        "core_problem", "methods", "mechanisms", "experimental_setup", "main_results",
        "limitations", "relevance_to_topic", "commonalities", "differences",
        "complementarities", "applicability",
    ):
        yield from getattr(value, name, [])


def _chunk_text(text: str) -> list[str]:
    """Split complete page text while retaining overlap and readable boundaries."""
    source = text.strip()
    if not source:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(source):
        hard_end = min(len(source), start + CHUNK_SIZE)
        end = hard_end
        if hard_end < len(source):
            window = source[start + CHUNK_SIZE // 2:hard_end]
            boundary = max(window.rfind(mark) for mark in ("\n", "。", ". ", "；", "; "))
            if boundary >= 0:
                end = start + CHUNK_SIZE // 2 + boundary + 1
        chunk = source[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(source):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _useful_section(value: str | None) -> str | None:
    if not value:
        return None
    title = " ".join(value.split()).strip()
    if not 3 <= len(title) <= 100:
        return None
    readable = sum(character.isalpha() or "\u4e00" <= character <= "\u9fff" for character in title)
    if readable / len(title) < 0.55 or re.search(r"(?:arg\s*min|^[=+\-]|[{}]{2})", title, re.IGNORECASE):
        return None
    return title


@dataclass(frozen=True)
class _SpecialistSpec:
    key: str
    label: str
    fields: tuple[str, ...]
    response_model: type[BaseModel]
    keywords: tuple[str, ...]
    max_text_chunks: int
    max_visuals: int
    instructions: str


SPECIALISTS = (
    _SpecialistSpec(
        "problem", "研究问题与贡献", ("core_problem", "relevance_to_topic"),
        ProblemAnalysisDraft,
        ("摘要", "引言", "背景", "问题", "目的", "挑战", "贡献", "abstract", "introduction",
         "problem", "challenge", "objective", "contribution", "propose"),
        7, 0,
        "说明研究背景、现有方法缺口、论文要解决的精确问题、主要贡献及其与用户课题的联系。",
    ),
    _SpecialistSpec(
        "method", "方法与作用机制", ("methods", "mechanisms"), MethodAnalysisDraft,
        ("方法", "算法", "模型", "流程", "公式", "步骤", "框架", "method", "algorithm",
         "model", "framework", "equation", "architecture", "processing"),
        8, 3,
        "讲清方法本身与作用机制：输入形式、主要处理步骤与关键组件、输出，以及机制为何能产生"
        "目标结果。每一条 value 必须是一句完整的具体论述（建议 2 个完整句子以上）；严禁把"
        "『输入』『处理步骤』『关键组件』『输出』『因果机制』这类分类词单独当作条目内容。"
        "methods 描述方法如何构成与运作，mechanisms 描述其有效的因果原理，两者内容不得相同。"
        "若论文明确阐述了机制（如某一步骤为何有效），mechanisms 应使用 kind=supported 并引用"
        "对应证据；只有超出论文论述的推断才使用 inference。",
    ),
    _SpecialistSpec(
        "experiment", "实验设置与结果", ("experimental_setup", "main_results"),
        ExperimentAnalysisDraft,
        ("实验", "数据", "指标", "结果", "比较", "验证", "精度", "消融", "experiment",
         "dataset", "metric", "result", "baseline", "comparison", "accuracy", "ablation"),
        8, 5,
        "每个栏目写 2-3 条。说明数据来源、样本或场景、参数、基线、评价指标和实验流程；"
        "优先报告数值结果、对比和图表趋势，每条控制在 180 个汉字以内。",
    ),
    _SpecialistSpec(
        "critical", "局限与适用条件", ("limitations",), CriticalAnalysisDraft,
        ("讨论", "结论", "局限", "不足", "未来", "适用", "discussion", "conclusion",
         "limitation", "future", "applicable", "uncertainty"),
        7, 2,
        "区分作者明确承认的局限与基于证据的审慎推断，并说明适用范围、前提和潜在失败情形。"
        "每一条 value 必须写明具体的局限或适用条件；严禁把『作者明确承认的局限』"
        "『基于证据的审慎推断』等类别短语当作条目内容。",
    ),
)


class PaperAnalyst:
    name = "paper_analyst"

    def __init__(
        self,
        provider: LLMProvider,
        documents: DocumentService,
        document_repository: DocumentRepository,
        evidence: EvidenceRepository,
        research: ResearchDataRepository,
        work_items: WorkItemRepository,
        skills: SkillRegistry,
        settings: Settings | None = None,
    ) -> None:
        self.provider = provider
        self.documents = documents
        self.document_repository = document_repository
        self.evidence = evidence
        self.research = research
        self.work_items = work_items
        self.skills = skills
        self.settings = settings or get_settings()

    async def run(
        self,
        project_id: str,
        revision: int,
        paper,
        linked,
        topic: str,
        requirements: str | None,
    ) -> PaperAnalysis:
        pipeline = DocumentAnalysisPipeline(
            self.documents, self.document_repository, self.evidence, self.research, self.provider
        )
        pending = {item["id"] for item in await self.research.pending_documents(project_id)}
        if linked.document_id in pending:
            await pipeline.run(project_id, paper.id, linked.document_id, linked.sha256)

        await self.research.set_analysis_step(project_id, "正在建立全文证据索引")
        parsed = self.documents.get_structure(project_id, linked.document_id)
        text_nodes = await self._build_full_text_index(project_id, paper.id, linked.document_id, parsed)
        all_evidence = await self.evidence.list_for_paper(project_id, paper.id, limit=500)
        visual_nodes = [item for item in all_evidence if item.evidence_type != "text"]
        allowed = {item.evidence_id for item in [*text_nodes, *visual_nodes]}
        method_skill = self.skills.load_skill("method-mechanism-extraction")

        fingerprint = hashlib.sha256(json.dumps({
            "version": ANALYSIS_PIPELINE_VERSION,
            "document_sha": linked.sha256,
            "topic": topic,
            "requirements": requirements,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        run_scope = f"paper-analysis:{revision}:{paper.id}:{fingerprint[:16]}"

        drafts: dict[str, BaseModel] = {}
        semaphore = asyncio.Semaphore(self.settings.paper_analysis_concurrency)
        for batch_start in range(0, len(SPECIALISTS), self.settings.paper_analysis_concurrency):
            batch = SPECIALISTS[
                batch_start:batch_start + self.settings.paper_analysis_concurrency
            ]
            end = batch_start + len(batch)
            await self.research.set_analysis_step(
                project_id, f"正在深入分析第 {batch_start + 1}-{end}/4 部分"
            )
            values = await asyncio.gather(*[
                self._run_specialist(
                    semaphore, project_id, run_scope, paper, topic, requirements,
                    spec, text_nodes, visual_nodes, allowed,
                    method_skill.content if spec.key == "method" else None,
                )
                for spec in batch
            ])
            drafts.update({spec.key: value for spec, value in zip(batch, values, strict=True)})

        draft = PaperAnalysis(
            paper_id=paper.id,
            title=paper.metadata.title,
            overview=None,
            core_problem=drafts["problem"].core_problem,
            methods=drafts["method"].methods,
            mechanisms=drafts["method"].mechanisms,
            experimental_setup=drafts["experiment"].experimental_setup,
            main_results=drafts["experiment"].main_results,
            limitations=drafts["critical"].limitations,
            relevance_to_topic=drafts["problem"].relevance_to_topic,
        )
        self._sanitize_evidence(draft, allowed)

        await self.research.set_analysis_step(project_id, "正在生成综合概述（5/5）")
        overview = await self._run_overview(
            project_id, run_scope, paper, topic, requirements, draft, allowed
        )
        draft.overview = overview.overview
        self._sanitize_evidence(draft, allowed)
        issues = self._quality_issues(draft)
        if issues:
            raise LLMError("论文分析未通过质量检查：" + "；".join(issues))
        await self.research.set_analysis_step(project_id, "论文深度分析已完成")
        return draft

    async def _build_full_text_index(
        self, project_id: str, paper_id: str, document_id: str, parsed
    ) -> list[EvidenceNode]:
        builder = EvidenceBuilder(self.documents, self.document_repository, self.evidence)
        nodes: list[EvidenceNode] = []
        for page in parsed.pages:
            for position, quote in enumerate(_chunk_text(page.text), 1):
                nodes.append(await builder.build_text(project_id, TextEvidenceCreate(
                    paper_id=paper_id,
                    document_id=document_id,
                    page_number=page.page_number,
                    claim=f"全文证据块（第 {page.page_number} 页，第 {position} 段）",
                    quote=quote,
                    confidence=1,
                )))
        if not nodes:
            raise LLMError("PDF 没有可用于分析的正文文本")
        return nodes

    async def _run_specialist(
        self,
        semaphore: asyncio.Semaphore,
        project_id: str,
        run_scope: str,
        paper,
        topic: str,
        requirements: str | None,
        spec: _SpecialistSpec,
        text_nodes: list[EvidenceNode],
        visual_nodes: list[EvidenceNode],
        allowed: set[str],
        skill_content: str | None,
    ) -> BaseModel:
        selected = self._select_evidence(spec, text_nodes, visual_nodes, topic)
        evidence_json = json.dumps([self._compact(item) for item in selected], ensure_ascii=False)
        system = (
            "你是严谨的论文精读专家。请使用简体中文输出详细、可教学的分析。每个栏目给出 2-5 条"
            "实质性论述，每条用 2-3 个完整句子说明论文做了什么、如何实现、为什么重要。"
            "论文明确陈述的事实必须使用 kind=supported 并仅引用所给 evidence_id；分析性判断使用"
            " kind=inference 且 evidence_ids 为空。不要把摘要换一种说法，也不要虚构未报告的数值。"
            + (f"\n\n必须遵循以下方法机制提取规范：\n{skill_content}" if skill_content else "")
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"分析任务：{spec.label}\n具体要求：{spec.instructions}\n"
                f"用户课题：{topic}\n用户分析要求：{requirements or '无额外要求'}\n"
                f"论文元数据：{paper.metadata.model_dump_json()}\n证据：{evidence_json}"
            )},
        ]
        output_tokens = (
            self.settings.paper_analysis_experiment_tokens
            if spec.key == "experiment"
            else self.settings.paper_analysis_section_tokens
        )
        async with semaphore:
            result = await self._checkpointed_call(
                project_id, run_scope, spec.key, messages, spec.response_model,
                output_tokens,
            )
        self._sanitize_evidence(result, allowed)
        repair_round = 0
        while self._fields_need_repair(result, spec.fields) and repair_round < 2:
            repair_round += 1
            guidance = (
                "上一次结果过短或缺少有证据支持的解释，且条目里出现的是分类标签而不是具体内容。"
                "请只重写该结构：每个栏目至少 2 条，每条 value 必须是一段不少于 30 字的具体论述，"
                "说明论文做了什么、如何实现、为什么，禁止把栏目名或分类词单独作为条目内容。"
                if repair_round == 1 else
                f"第二次修复（栏目：{'、'.join(spec.fields)}）仍不合格。请彻底重写：每条 value "
                "扩写到 50 字以上并包含具体细节（方法组件名称、机制因果或局限情形），"
                "不得保留标签式短句，也不得复述上一轮内容。"
            )
            repair_messages = [
                *messages,
                {"role": "assistant", "content": result.model_dump_json()},
                {"role": "user", "content": guidance},
            ]
            async with semaphore:
                result = await self._checkpointed_call(
                    project_id, run_scope, f"{spec.key}-repair", repair_messages,
                    spec.response_model, output_tokens,
                )
            self._sanitize_evidence(result, allowed)
        return result

    async def _run_overview(
        self, project_id: str, run_scope: str, paper, topic: str,
        requirements: str | None, draft: PaperAnalysis, allowed: set[str],
    ) -> PaperOverviewDraft:
        messages = [
            {"role": "system", "content": (
                "用简体中文撰写一段连贯的论文综合概述。必须用 5-8 个完整句子依次讲清研究问题、"
                "核心方法、作用机制、实验依据、主要结论、局限和对用户课题的意义。不要重复标题。"
                "事实使用 supported 并引用输入中已有 evidence_id；综合判断使用 inference 且不引用证据。"
            )},
            {"role": "user", "content": (
                f"用户课题：{topic}\n分析要求：{requirements or '无额外要求'}\n"
                f"论文：{paper.metadata.model_dump_json()}\n"
                f"分项分析：{draft.model_dump_json()}"
            )},
        ]
        result = await self._checkpointed_call(
            project_id, run_scope, "overview", messages, PaperOverviewDraft,
            self.settings.paper_analysis_overview_tokens,
        )
        self._sanitize_evidence(result, allowed)
        if len(result.overview.value.strip()) < 120:
            repair = [
                *messages,
                {"role": "assistant", "content": result.model_dump_json()},
                {"role": "user", "content": "概述过短。请扩写为信息密集、逻辑连贯的 5-8 个完整句子。"},
            ]
            result = await self._checkpointed_call(
                project_id, run_scope, "overview-repair", repair, PaperOverviewDraft,
                self.settings.paper_analysis_overview_tokens,
            )
            self._sanitize_evidence(result, allowed)
        return result

    async def _checkpointed_call(
        self,
        project_id: str,
        run_scope: str,
        item_key: str,
        messages: list[dict[str, str]],
        response_model: type[DraftT],
        max_output_tokens: int,
    ) -> DraftT:
        input_hash = hashlib.sha256(json.dumps({
            "messages": messages,
            "model": response_model.__name__,
            "max_output_tokens": max_output_tokens,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        item, should_run = await self.work_items.claim(
            project_id,
            run_scope,
            item_key,
            "paper_analysis_section",
            input_hash,
            replace_changed=True,
        )
        if not should_run:
            return response_model.model_validate(item.result)
        started = perf_counter()
        try:
            result = await self.provider.structured_output_bounded(
                messages, response_model, max_output_tokens=max_output_tokens
            )
            await self.work_items.complete(
                project_id, run_scope, item_key, result.model_dump(mode="json"),
                round((perf_counter() - started) * 1_000),
            )
            return result
        except Exception as exc:
            await self.work_items.fail(
                project_id, run_scope, item_key,
                {"type": type(exc).__name__, "message": str(exc)[:1_000]},
                round((perf_counter() - started) * 1_000),
            )
            raise

    @staticmethod
    def _select_evidence(
        spec: _SpecialistSpec,
        text_nodes: list[EvidenceNode],
        visual_nodes: list[EvidenceNode],
        topic: str,
    ) -> list[EvidenceNode]:
        max_page = max((item.page_number or 1 for item in text_nodes), default=1)
        topic_terms = [term.casefold() for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", topic)]

        def score(item: EvidenceNode) -> float:
            content = f"{item.section or ''} {item.claim} {item.excerpt or ''}".casefold()
            value = sum(content.count(term.casefold()) * 2 for term in spec.keywords)
            value += sum(1 for term in topic_terms if term in content)
            page = item.page_number or 1
            if spec.key == "problem" and page <= 2:
                value += 4
            elif spec.key == "method" and 1 < page < max_page:
                value += 2
            elif spec.key in {"experiment", "critical"} and page >= max(1, max_page - 2):
                value += 4
            return value

        chosen_text: list[EvidenceNode] = []
        per_page: dict[int, int] = {}
        for item in sorted(text_nodes, key=lambda value: (-score(value), value.page_number or 0)):
            page = item.page_number or 0
            if per_page.get(page, 0) >= 2:
                continue
            chosen_text.append(item)
            per_page[page] = per_page.get(page, 0) + 1
            if len(chosen_text) >= spec.max_text_chunks:
                break
        chosen_visuals = sorted(visual_nodes, key=score, reverse=True)[:spec.max_visuals]
        return [*chosen_text, *chosen_visuals]

    @staticmethod
    def _compact(item: EvidenceNode) -> dict[str, Any]:
        return {
            "evidence_id": item.evidence_id,
            "type": item.evidence_type,
            "page": item.page_number,
            "section": _useful_section(item.section),
            "claim": item.claim[:900],
            "excerpt": (item.excerpt or "")[:1_000],
        }

    @staticmethod
    def _sanitize_evidence(value: BaseModel, allowed: set[str]) -> None:
        for claim in _claims(value):
            if not set(claim.evidence_ids) <= allowed:
                claim.kind = "inference"
                claim.evidence_ids = []

    @staticmethod
    def _fields_need_repair(value: BaseModel, fields: tuple[str, ...]) -> bool:
        for field in fields:
            claims = getattr(value, field, [])
            chars = sum(len(claim.value.strip()) for claim in claims)
            sufficient = (len(claims) >= 2 and chars >= 100) or (
                len(claims) >= 1 and chars >= 300
            )
            if not sufficient:
                return True
            # mechanisms (causal interpretation) and limitations (judgment) may
            # legitimately be inference-only; factual sections need >=1 supported.
            if field not in {"mechanisms", "limitations"} and not any(
                claim.kind == "supported" for claim in claims
            ):
                return True
        return False

    @classmethod
    def _quality_issues(cls, value: PaperAnalysis) -> list[str]:
        issues: list[str] = []
        if value.overview is None or len(value.overview.value.strip()) < 120:
            issues.append("综合概述不足 120 字")
        for field, label in (
            ("core_problem", "核心问题"), ("methods", "方法"), ("mechanisms", "机制"),
            ("experimental_setup", "实验设置"), ("main_results", "主要结果"),
            ("limitations", "局限"), ("relevance_to_topic", "课题相关性"),
        ):
            if cls._fields_need_repair(value, (field,)):
                issues.append(f"{label}内容不足")
        return issues


class EvidenceSynthesizer:
    name = "evidence_synthesizer"

    def __init__(self, provider: LLMProvider, sessions: ResearchSessionRepository) -> None:
        self.provider = provider
        self.sessions = sessions

    async def run(
        self, project_id: str, revision: int, topic: str, requirements: str | None,
        papers: list[PaperAnalysis],
    ) -> AnalysisReport:
        comparison = None
        if len(papers) == 2:
            comparison = await self.provider.structured_output_bounded([
                {"role": "system", "content": (
                    "用简体中文详细比较两篇已经完成证据校验的论文。概述用 4-8 个句子说明两篇论文的"
                    "核心关系，并分别给出共同点、差异、互补性和适用条件。只能引用输入中存在的证据 ID。"
                    "事实使用 supported，跨论文解释使用 inference。"
                )},
                {"role": "user", "content": (
                    f"课题：{topic}\n分析要求：{requirements or '无额外要求'}\n"
                    + json.dumps([item.model_dump(mode="json") for item in papers],
                                 ensure_ascii=False)
                )},
            ], ComparisonDraft, max_output_tokens=1_024)
            allowed = {eid for paper in papers for claim in _claims(paper)
                       for eid in claim.evidence_ids}
            for claim in _claims(comparison):
                if not set(claim.evidence_ids) <= allowed:
                    claim.kind = "inference"
                    claim.evidence_ids = []
        from app.db.repositories import utc_now
        report = AnalysisReport(
            project_id=project_id, search_revision=revision,
            analysis_requirements=requirements, papers=papers,
            comparison=comparison, created_at=utc_now(),
        )
        await self.sessions.save_report(report)
        return report
