from app.core.config import Settings, get_settings
from app.db import DocumentRepository, EvidenceRepository, ResearchDataRepository
from app.documents.service import DocumentService
from app.evidence import EvidenceBuilder
from app.llm import LLMProvider
from app.schemas import FigureEvidenceCreate, TableEvidenceCreate, VisualObservation


class DocumentAnalysisPipeline:
    """Versioned PDF analysis that turns every detected visual region into evidence."""

    def __init__(self, service: DocumentService, documents: DocumentRepository,
                 evidence: EvidenceRepository, research: ResearchDataRepository,
                 provider: LLMProvider, settings: Settings | None = None) -> None:
        self.service, self.documents, self.evidence = service, documents, evidence
        self.research, self.provider = research, provider
        self.settings = settings or get_settings()

    async def run(self, project_id: str, paper_id: str, document_id: str,
                  input_hash: str) -> None:
        analysis_id = await self.research.start_document_analysis(
            project_id, document_id, input_hash, self.settings.ollama_vision_model)
        parsed = self.service.parse_document(project_id, document_id)
        builder = EvidenceBuilder(self.service, self.documents, self.evidence)
        visuals = [*(('figure', value) for value in parsed.figures),
                   *(('table', value) for value in parsed.tables
                     if value.source_path and value.sha256)]
        await self.research.start_visual_progress(analysis_id, len(visuals))
        semaphore = asyncio.Semaphore(self.settings.visual_analysis_concurrency)

        async def analyze_visual(region_type, visual) -> None:
            async with semaphore:
                path = self.service.workspace.resolve_safe_path(
                    project_id, visual.source_path
                )
                observation = await self._observe(
                    path.read_bytes(), visual.caption, region_type
                )
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
                if region_type == "figure":
                    await builder.build_figure(project_id, FigureEvidenceCreate(
                        paper_id=paper_id, document_id=document_id,
                        figure_id=visual.figure_id, claim=observation.summary,
                        confidence=observation.confidence,
                    ))
                else:
                    await builder.build_table(project_id, TableEvidenceCreate(
                        paper_id=paper_id, document_id=document_id,
                        table_id=visual.table_id, claim=observation.summary,
                        confidence=observation.confidence,
                    ))
                await self.research.advance_visual_progress(
                    analysis_id, f"已完成：{visual.label or region_type}"
                )
        try:
            await asyncio.gather(*(analyze_visual(*visual) for visual in visuals))
            await self.research.finish_document_analysis(
                analysis_id, parsed.page_count, parsed.warnings)
        except Exception as exc:
            await self.research.finish_document_analysis(analysis_id, parsed.page_count,
                parsed.warnings, {"type": type(exc).__name__, "message": str(exc)[:1000]})
            raise

    async def _observe(self, image: bytes, caption: str | None,
                       region_type: str) -> VisualObservation:
        return await self.provider.structured_output_with_images([{
            "role": "user", "content": (
                "Analyze this academic paper visual. Report only directly visible facts; put "
                "anything uncertain in unknowns. Do not claim novelty. "
                f"Region type: {region_type}. Caption: {caption or 'not available'}"
            )}], [image], VisualObservation)
import asyncio
