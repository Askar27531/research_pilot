import hashlib
import logging
import re
from uuid import uuid4

import pymupdf

from app.documents.errors import DocumentParseError
from app.documents.workspace import WorkspaceManager
from app.schemas import (
    BoundingBox,
    DocumentFigure,
    DocumentPage,
    DocumentSection,
    ParsedDocument,
    TableCandidate,
)

HEADING_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:abstract|introduction|related work|method(?:ology)?|"
    r"experiments?|results?|discussion|conclusion|references)\b",
    re.IGNORECASE,
)
FIGURE_PATTERN = re.compile(r"^(?:fig(?:ure)?\.?)[ ]*(\d+[A-Za-z]?)\b", re.IGNORECASE)
TABLE_PATTERN = re.compile(r"^table[ ]*(\d+[A-Za-z]?)\b", re.IGNORECASE)
logger = logging.getLogger(__name__)


class PDFParser:
    def __init__(
        self, workspace: WorkspaceManager, *, render_dpi: int = 144, ocr_languages: str = "eng"
    ) -> None:
        self.workspace = workspace
        self.render_dpi = render_dpi
        self.ocr_languages = ocr_languages

    def parse(self, project_id: str, document_id: str) -> ParsedDocument:
        entry = self.workspace.get_document(project_id, document_id)
        source = self.workspace.resolve_safe_path(project_id, entry.relative_path)
        try:
            document = pymupdf.open(source)
        except Exception as exc:
            raise DocumentParseError(f"Cannot open PDF: {exc}") from exc
        pages: list[DocumentPage] = []
        warnings: list[str] = []
        figures: list[DocumentFigure] = []
        tables: list[TableCandidate] = []
        headings: list[tuple[str, int, float]] = []
        seen_images: set[str] = set()
        try:
            for index in range(document.page_count):
                page_number = index + 1
                try:
                    page = document.load_page(index)
                    text = page.get_text("text", sort=True)
                    if len(text.strip()) < 40:
                        try:
                            text_page = page.get_textpage_ocr(
                                language=self.ocr_languages, dpi=self.render_dpi, full=False
                            )
                            text = page.get_text("text", textpage=text_page, sort=True)
                        except Exception as exc:  # noqa: BLE001 - vision handles scan fallback
                            logger.debug("OCR unavailable for page %s: %s", page_number, exc)
                    screenshot = self._render_page(project_id, document_id, page, page_number)
                    pages.append(
                        DocumentPage(
                            document_id=document_id,
                            page_number=page_number,
                            text=text,
                            width=page.rect.width,
                            height=page.rect.height,
                            rotation=page.rotation,
                            screenshot_path=screenshot,
                        )
                    )
                    headings.extend(self._find_headings(page, page_number))
                    figures.extend(
                        self._extract_page_figures(
                            project_id, document_id, document, page, page_number, seen_images
                        )
                    )
                    figures.extend(self._extract_vector_regions(
                        project_id, document_id, page, page_number
                    ))
                    tables.extend(self._find_tables(
                        project_id, document_id, page, page_number
                    ))
                except Exception as exc:  # noqa: BLE001 - isolate a corrupt page and continue
                    warnings.append(f"Page {page_number} failed: {type(exc).__name__}: {exc}")
                    rect = document.load_page(index).rect
                    pages.append(
                        DocumentPage(
                            document_id=document_id,
                            page_number=page_number,
                            text="",
                            width=rect.width,
                            height=rect.height,
                            rotation=0,
                            error=str(exc)[:1_000],
                        )
                    )
            parsed = ParsedDocument(
                document_id=document_id,
                project_id=project_id,
                title=document.metadata.get("title") or None,
                author=document.metadata.get("author") or None,
                page_count=document.page_count,
                pages=pages,
                sections=self._build_sections(headings, document.page_count),
                figures=figures,
                tables=tables,
                warnings=warnings,
            )
        finally:
            document.close()
        output = self.workspace.resolve_safe_path(project_id, f"parsed/{document_id}/document.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
        return parsed

    def _render_page(
        self, project_id: str, document_id: str, page: pymupdf.Page, page_number: int
    ) -> str:
        relative = f"parsed/{document_id}/pages/page-{page_number:04d}.png"
        path = self.workspace.resolve_safe_path(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = page.get_pixmap(dpi=self.render_dpi, alpha=False)
        pixmap.save(path)
        return relative

    def _extract_page_figures(
        self,
        project_id: str,
        document_id: str,
        document: pymupdf.Document,
        page: pymupdf.Page,
        page_number: int,
        seen_images: set[str],
    ) -> list[DocumentFigure]:
        results: list[DocumentFigure] = []
        for image in page.get_images(full=True):
            xref, width, height = image[0], image[2], image[3]
            if width < 80 or height < 80 or width * height < 20_000:
                continue
            extracted = document.extract_image(xref)
            content = extracted["image"]
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen_images:
                continue
            seen_images.add(digest)
            rects = page.get_image_rects(xref)
            rect = rects[0] if rects else page.rect
            caption, label = self._nearest_caption(page, rect, FIGURE_PATTERN)
            figure_id = str(uuid4())
            extension = extracted.get("ext", "png")
            relative = f"parsed/{document_id}/figures/{figure_id}.{extension}"
            path = self.workspace.resolve_safe_path(project_id, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            results.append(
                DocumentFigure(
                    figure_id=figure_id,
                    document_id=document_id,
                    page_number=page_number,
                    label=label,
                    caption=caption,
                    figure_type=self._classify_figure(caption),
                    bbox=BoundingBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                    source_path=relative,
                    sha256=digest,
                )
            )
        return results

    def _extract_vector_regions(
        self, project_id: str, document_id: str, page: pymupdf.Page, page_number: int
    ) -> list[DocumentFigure]:
        cluster = getattr(page, "cluster_drawings", None)
        if cluster is None:
            return []
        results: list[DocumentFigure] = []
        try:
            regions = cluster(drawings=page.get_drawings())
        except Exception:  # noqa: BLE001 - vector extraction is best effort
            return []
        for rect in regions:
            rect = pymupdf.Rect(rect)
            if rect.width < 100 or rect.height < 80 or rect.get_area() < 20_000:
                continue
            caption, label = self._nearest_caption(page, rect, FIGURE_PATTERN)
            if not caption:
                continue
            figure_id = str(uuid4())
            relative = f"parsed/{document_id}/figures/{figure_id}.png"
            path = self.workspace.resolve_safe_path(project_id, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = page.get_pixmap(dpi=self.render_dpi, alpha=False, clip=rect).tobytes("png")
            path.write_bytes(content)
            results.append(DocumentFigure(
                figure_id=figure_id, document_id=document_id, page_number=page_number,
                label=label, caption=caption, figure_type=self._classify_figure(caption),
                bbox=BoundingBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                source_path=relative, sha256=hashlib.sha256(content).hexdigest(),
                extraction_method="vector_region",
            ))
        return results

    @staticmethod
    def _nearest_caption(
        page: pymupdf.Page, rect: pymupdf.Rect, pattern: re.Pattern[str]
    ) -> tuple[str | None, str | None]:
        candidates: list[tuple[float, str, str]] = []
        for block in page.get_text("blocks"):
            text = " ".join(str(block[4]).split())
            match = pattern.search(text)
            if not match:
                continue
            block_rect = pymupdf.Rect(block[:4])
            distance = min(abs(block_rect.y0 - rect.y1), abs(rect.y0 - block_rect.y1))
            candidates.append((distance, text, match.group(0)))
        if not candidates:
            return None, None
        _, caption, label = min(candidates, key=lambda item: item[0])
        return caption[:2_000], label

    @staticmethod
    def _classify_figure(caption: str | None) -> str:
        value = (caption or "").casefold()
        if any(word in value for word in ("architecture", "framework", "pipeline", "network")):
            return "architecture"
        if any(word in value for word in ("ablation", "component analysis")):
            return "ablation"
        if any(word in value for word in ("result", "comparison", "performance", "qualitative")):
            return "result"
        return "other"

    @staticmethod
    def _find_headings(page: pymupdf.Page, page_number: int) -> list[tuple[str, int, float]]:
        lines: list[tuple[str, float]] = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = " ".join(str(span.get("text", "")).strip() for span in spans).strip()
                if text:
                    lines.append((text, max(float(span.get("size", 0)) for span in spans)))
        if not lines:
            return []
        sizes = sorted(size for _, size in lines)
        median = sizes[len(sizes) // 2]
        return [
            (text[:500], page_number, min(1.0, 0.7 + max(size - median, 0) / 20))
            for text, size in lines
            if len(text) <= 160
            and len(text.split()) <= 18
            and sum(character.isalpha() for character in text) >= max(3, len(text) * 0.4)
            and (HEADING_PATTERN.match(text) or size >= max(12, median * 1.25))
        ]

    @staticmethod
    def _build_sections(
        headings: list[tuple[str, int, float]], page_count: int
    ) -> list[DocumentSection]:
        sections: list[DocumentSection] = []
        for index, (title, start, confidence) in enumerate(headings):
            next_start = headings[index + 1][1] if index + 1 < len(headings) else page_count
            sections.append(
                DocumentSection(
                    title=title,
                    start_page=start,
                    end_page=max(start, next_start),
                    confidence=confidence,
                )
            )
        return sections

    def _find_tables(
        self, project_id: str, document_id: str, page: pymupdf.Page, page_number: int
    ) -> list[TableCandidate]:
        results: list[TableCandidate] = []
        try:
            found = page.find_tables()
        except Exception:  # noqa: BLE001 - table extraction is best effort
            found = None
        if found is not None:
            for table in found.tables:
                rect = pymupdf.Rect(table.bbox)
                caption, label = self._nearest_caption(page, rect, TABLE_PATTERN)
                table_id = str(uuid4())
                relative = f"parsed/{document_id}/tables/{table_id}.png"
                path = self.workspace.resolve_safe_path(project_id, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                content = page.get_pixmap(
                    dpi=self.render_dpi, alpha=False, clip=rect
                ).tobytes("png")
                path.write_bytes(content)
                results.append(TableCandidate(
                    table_id=table_id, document_id=document_id, page_number=page_number,
                    label=label, caption=caption,
                    bbox=BoundingBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
                    cells=table.extract(), source_path=relative,
                    sha256=hashlib.sha256(content).hexdigest(),
                ))
        if results:
            return results
        for block in page.get_text("blocks"):
            text = " ".join(str(block[4]).split())
            match = TABLE_PATTERN.search(text)
            if match:
                results.append(
                    TableCandidate(
                        table_id=str(uuid4()),
                        document_id=document_id,
                        page_number=page_number,
                        label=match.group(0),
                        caption=text[:2_000],
                        bbox=BoundingBox(x0=block[0], y0=block[1], x1=block[2], y1=block[3]),
                    )
                )
        return results
