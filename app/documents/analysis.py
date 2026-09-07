import asyncio
import hashlib
import logging
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.db import DocumentRepository, EvidenceRepository, ResearchDataRepository
from app.documents.parser import unmatched_figure_references
from app.documents.service import DocumentService
from app.evidence import EvidenceBuilder
from app.evidence.consistency import CrossModalConsistencyChecker
from app.llm import LLMProvider
from app.reliability.faults import AnalysisPausedError
from app.schemas import (
    BoundingBox,
    DocumentFigure,
    FigureEvidenceCreate,
    TableEvidenceCreate,
    VisualObservation,
)
from app.skills import SkillRegistry

logger = logging.getLogger(__name__)

# Behavior comes from the visual-observation skill; this constant is only the
# no-registry fallback so the pipeline never runs with an empty instruction.
FALLBACK_OBSERVATION_SYSTEM = (
    "Analyze this academic paper visual. Report only directly visible facts; put "
    "anything uncertain in unknowns. Do not claim novelty."
)


def _observation_context(region_type: str, caption: str | None, label: str | None,
                         mentions) -> str:
    """Assemble the *structural* user context (no behavioral rules here).

    Behavioral guidance (fact-first, no caption-as-evidence, unknowns discipline)
    is defined by the visual-observation skill; this function only serializes the
    inputs the skill needs: region type, label, caption and prose mentions.
    """
    lines = [f"Region: {region_type}. Label: {label or 'not available'}",
             f"Caption: {caption or 'not available'}"]
    context: list[str] = []
    seen: set[str] = set()
    for mention in (mentions or []):
        sentence = " ".join(mention.sentence.split())
        if not sentence or sentence in seen:
            continue
        seen.add(sentence)
        context.append(f"(p{mention.page_number}) {sentence}")
        if len(context) >= 4:
            break
    if context:
        lines.append("Prose mentions of this visual:\n- " + "\n- ".join(context))
    return "\n".join(lines)


class DocumentAnalysisPipeline:
    """Versioned PDF analysis that turns every detected visual region into evidence.

    The "how to look at a figure" methodology is governed by the
    ``visual-observation`` skill; this class owns only the structural pipeline
    (parse -> observe -> persist region/evidence).
    """

    def __init__(self, service: DocumentService, documents: DocumentRepository,
                 evidence: EvidenceRepository, research: ResearchDataRepository,
                 provider: LLMProvider, skills: SkillRegistry | None = None,
                 settings: Settings | None = None) -> None:
        self.service, self.documents, self.evidence = service, documents, evidence
        self.research, self.provider = research, provider
        self.skills = skills
        self.settings = settings or get_settings()
        self.observation_system = self._load_observation_system()

    def _load_observation_system(self) -> str:
        if self.skills is None:
            return FALLBACK_OBSERVATION_SYSTEM
        try:
            return self.skills.load_skill("visual-observation").content
        except Exception:
            logger.warning("visual-observation skill unavailable; using fallback prompt",
                           exc_info=True)
            return FALLBACK_OBSERVATION_SYSTEM

    async def _pause_reason(self, project_id: str) -> str | None:
        """``user`` / ``budget_gate`` when a safe boundary must stop the run."""
        return await self.research.budget_pause_reason(project_id, self.settings)

    @staticmethod
    def _pause_message(reason: str) -> str:
        if reason == "budget_gate":
            return "已达运行预算门槛，分析在图表边界暂停，等待人工决定是否继续"
        return "用户请求在图表边界暂停分析"

    async def run(self, project_id: str, paper_id: str, document_id: str,
                  input_hash: str) -> None:
        analysis_id = await self.research.start_document_analysis(
            project_id, document_id, input_hash, self.settings.ollama_vision_model)
        parsed = self.service.parse_document(project_id, document_id)
        if self.settings.figure_repair_enabled:
            self._apply_figure_repairs(project_id, parsed)
        builder = EvidenceBuilder(self.service, self.documents, self.evidence)
        consistency_checker = None
        if self.settings.cross_modal_consistency_enabled:
            try:
                consistency_checker = CrossModalConsistencyChecker(
                    self.provider, self.service, self.research, skills=self.skills
                )
            except Exception:
                logger.warning("consistency checker unavailable", exc_info=True)
        visuals = [*(('figure', value) for value in parsed.figures),
                   *(('table', value) for value in parsed.tables
                     if value.source_path and value.sha256)]
        await self.research.start_visual_progress(analysis_id, len(visuals))
        concurrency = self.settings.visual_analysis_concurrency
        # Cooperative pause/resume mid-document: visuals finished before the
        # pause already have persisted regions, so a resume skips them instead
        # of re-paying their vision calls.
        existing_keys = await self.research.visual_keys(analysis_id)
        for _ in existing_keys:  # restore the persisted progress counters
            await self.research.advance_visual_progress(
                analysis_id, "恢复暂停前已完成的图表进度"
            )
        await self.research.set_progress(
            project_id, paper_id, "visuals", "running",
            label="图表视觉观察与一致性检查", done=len(existing_keys),
            total=len(visuals),
        )
        items: list[tuple[str, str, object]] = [
            (f"{visual.page_number}|{region_type}|{visual.label or ''}",
             region_type, visual)
            for region_type, visual in visuals
        ]
        done_count = len(existing_keys)
        lock = asyncio.Lock()

        async def analyze_visual(key: str, region_type: str, visual) -> None:
            nonlocal done_count
            if key in existing_keys:
                return  # already observed before the pause
            reason = await self._pause_reason(project_id)
            if reason:
                raise AnalysisPausedError(
                    self._pause_message(reason), reason=reason
                )
            path = self.service.workspace.resolve_safe_path(
                project_id, visual.source_path
            )
            observation = await self._observe(path.read_bytes(), visual, region_type)
            bbox = (
                visual.bbox.model_dump()
                if visual.bbox else {}
            )
            payload = observation.model_dump(mode="json")
            if region_type == "table":
                payload["cells"] = visual.cells
            await self.research.save_visual_region(
                analysis_id, project_id, document_id, region_type,
                visual.page_number, visual.label, bbox, visual.source_path,
                visual.sha256, payload,
            )
            evidence_node = None
            if region_type == "figure":
                evidence_node = await builder.build_figure(project_id, FigureEvidenceCreate(
                    paper_id=paper_id, document_id=document_id,
                    figure_id=visual.figure_id, claim=observation.summary,
                    confidence=observation.confidence,
                ))
            else:
                evidence_node = await builder.build_table(project_id, TableEvidenceCreate(
                    paper_id=paper_id, document_id=document_id,
                    table_id=visual.table_id, claim=observation.summary,
                    confidence=observation.confidence,
                ))
            if consistency_checker is not None and evidence_node is not None:
                await consistency_checker.check_evidence(
                    project_id, parsed, evidence_node
                )
            await self.research.advance_visual_progress(
                analysis_id, f"已完成：{visual.label or region_type}"
            )
            async with lock:
                done_count += 1
                await self.research.set_progress(
                    project_id, paper_id, "visuals", "running",
                    label="图表视觉观察与一致性检查", done=done_count,
                )

        paused = False
        pause_exc: AnalysisPausedError | None = None
        first_error: Exception | None = None
        tasks: set[asyncio.Task] = set()

        def cancel_tasks() -> None:
            for task in tasks:
                task.cancel()

        try:
            while items or tasks:
                while (
                    items
                    and len(tasks) < concurrency
                    and not paused
                    and first_error is None
                ):
                    key, region_type, visual = items.pop(0)
                    tasks.add(asyncio.create_task(
                        analyze_visual(key, region_type, visual)
                    ))
                if not tasks:
                    break
                done, tasks = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        await task
                    except AnalysisPausedError as exc:
                        if pause_exc is None:
                            pause_exc = exc  # first stop wins; carry its reason
                        paused = True  # drain in-flight visuals, then stop
                    except Exception as exc:  # noqa: BLE001 - recorded below
                        if first_error is None:
                            first_error = exc
            if first_error is not None:
                cancel_tasks()
                if tasks:
                    await asyncio.wait(tasks)
                await self.research.set_progress(
                    project_id, paper_id, "visuals", "failed",
                    label="图表视觉观察与一致性检查", done=done_count,
                )
                await self.research.finish_document_analysis(
                    analysis_id, parsed.page_count, parsed.warnings,
                    {"type": type(first_error).__name__,
                     "message": str(first_error)[:1000]},
                )
                raise first_error
            if paused:
                raise pause_exc or AnalysisPausedError(
                    self._pause_message("user"), reason="user"
                )
            await self.research.set_progress(
                project_id, paper_id, "visuals", "completed",
                label="图表视觉观察与一致性检查", done=len(visuals),
                total=len(visuals),
            )
            await self.research.finish_document_analysis(
                analysis_id, parsed.page_count, parsed.warnings)
        except asyncio.CancelledError:
            cancel_tasks()
            raise

    def _apply_figure_repairs(self, project_id: str, parsed) -> None:
        """P2-a repair loop (gated by ``figure_repair_enabled``).

        For every prose-referenced figure the deterministic parser could not
        localize, register the whole mentioning page as a figure stand-in so the
        vision pass still reads it (page-level localization). Stand-ins are
        persisted into document.json so evidence builders and later runs agree.
        """
        existing = {(figure.label or "").casefold() for figure in parsed.figures}
        pages = {page.page_number: page for page in parsed.pages if page.screenshot_path}
        added = False
        for label, page_number in unmatched_figure_references(parsed.pages, parsed.figures):
            if label.casefold() in existing:
                continue
            page = pages.get(page_number)
            if page is None:
                continue
            path = self.service.workspace.resolve_safe_path(project_id, page.screenshot_path)
            if not path.is_file():
                continue
            parsed.figures.append(DocumentFigure(
                figure_id=str(uuid4()),
                document_id=parsed.document_id,
                page_number=page_number,
                label=label,
                caption=None,
                figure_type="other",
                bbox=BoundingBox(x0=0, y0=0, x1=page.width, y1=page.height),
                source_path=page.screenshot_path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            ))
            existing.add(label.casefold())
            added = True
            logger.info("figure repair: localizing %s at page %s", label, page_number)
        if added:
            output = self.service.workspace.resolve_safe_path(
                project_id, f"parsed/{parsed.document_id}/document.json"
            )
            output.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")

    async def _observe(self, image: bytes, visual, region_type: str) -> VisualObservation:
        messages = [
            {"role": "system", "content": self.observation_system},
            {"role": "user", "content": _observation_context(
                region_type,
                getattr(visual, "caption", None),
                getattr(visual, "label", None),
                getattr(visual, "mentions", None),
            )},
        ]
        return await self.provider.structured_output_with_images(
            messages, [image], VisualObservation
        )
