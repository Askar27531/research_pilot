"""Deterministic document parsing that turns a PDF into a structured reading base.

Stage-1 goals (P1):
- Keep *layout-aware* text: blocks in reading order, each with a typographic
  role (body/heading/caption/header/footer/page_number), headers/footers and
  page numbers removed from the analysis text.
- Attach semantic section types (abstract/background/method/experiment/
  discussion/references) so downstream routing is not limited to page numbers.
- Index prose mentions of figures/tables (incl. sub-figures like "Fig. 3(a)",
  supplementary "Fig. S1", and compound references "Figs. 3 and 4").
- Cache the parsed result keyed by the source PDF SHA-256.

Parsing stays deterministic and model-free; model-assisted repair is a separate
(P2) capability.
"""

import hashlib
import logging
import re
import statistics
from uuid import uuid4

import pymupdf

from app.documents.errors import DocumentParseError
from app.documents.workspace import WorkspaceManager
from app.schemas import (
    BoundingBox,
    DocumentBlock,
    DocumentFigure,
    DocumentPage,
    DocumentSection,
    FigureMention,
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
# Prose references to figures, supporting sub-figure markers, supplementary
# numbers (S1) and compound references ("Figs. 3 and 4").
FIGURE_REF_RE = re.compile(
    r"\b(?:fig(?:ures?|s)?\.?)\s*(S?\d+(?:\(?[a-z]\)?)?)\b", re.IGNORECASE
)
FIGURE_CONTINUATION_RE = re.compile(
    r"\s*(?:,|;|&|and|to|–|—|-)\s*(S?\d+(?:\(?[a-z]\)?)?)\b", re.IGNORECASE
)
TABLE_REF_RE = re.compile(r"\btables?\.?\s*(\d+[A-Za-z]?)\b", re.IGNORECASE)
MENTION_CONTEXT_BEFORE = 160
MENTION_CONTEXT_AFTER = 240
MENTION_SNIPPET_LIMIT = 400
MENTION_COMPOUND_WINDOW = 90
MAX_MENTIONS_PER_VISUAL = 6
PAGE_NUMBER_RE = re.compile(r"^[-–—·\s]*\d+[-–—·\s]*$")
ROMAN_PAGE_NUMBER_RE = re.compile(r"^[-–—·\s]*[ivxlcdm]+[-–—·\s]*$", re.IGNORECASE)
logger = logging.getLogger(__name__)

BlockRecord = tuple[float, float, float, float, str, float]


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable)
# ---------------------------------------------------------------------------
def _visual_key(label: str | None) -> str | None:
    """Normalize a caption label like 'Fig. 3b' / 'Table 2' to a join key ('3b')."""
    if not label:
        return None
    match = re.search(r"(\d+[A-Za-z]?)", label)
    return match.group(1).lower() if match else None


def _core_key(key: str | None) -> str | None:
    """Strip a sub-figure letter so '3a' can fall back to the parent figure '3'."""
    if not key:
        return None
    match = re.match(r"(\d+)", key)
    return match.group(1) if match else None


def _mention_snippet(text: str, start: int) -> str:
    window = text[max(0, start - MENTION_CONTEXT_BEFORE): start + MENTION_CONTEXT_AFTER]
    return " ".join(window.split())[:MENTION_SNIPPET_LIMIT]


def _is_page_number(text: str) -> bool:
    stripped = text.strip()
    if len(stripped.split()) > 8:
        return False
    return bool(PAGE_NUMBER_RE.match(stripped) or ROMAN_PAGE_NUMBER_RE.match(stripped))


def _is_heading(text: str, size: float, median: float) -> bool:
    stripped = text.strip()
    words = stripped.split()
    if len(stripped) > 160 or len(words) > 18:
        return False
    # Section titles do not end with sentence punctuation; body sentences do.
    # Without this guard a paragraph that begins with "Results ..." would be
    # misread as a heading by HEADING_PATTERN.
    if stripped.endswith((".", "。")):
        return False
    readable = sum(character.isalpha() or "\u4e00" <= character <= "\u9fff"
                   for character in stripped)
    if readable < max(3, len(stripped) * 0.4):
        return False
    return bool(HEADING_PATTERN.match(stripped)) or size >= max(12, median * 1.25)


def _is_caption(text: str) -> bool:
    stripped = " ".join(text.split())
    return bool(FIGURE_PATTERN.match(stripped) or TABLE_PATTERN.match(stripped))


def _section_type_for_title(title: str) -> str:
    low = " ".join(title.split()).casefold()
    if low == "abstract" or low.startswith("abstract "):
        return "abstract"
    if any(word in low for word in (
        "introduction", "related work", "background", "preliminar", "motivation"
    )):
        return "background"
    if any(word in low for word in (
        "method", "approach", "model", "framework", "algorithm", "architecture", "system"
    )):
        return "method"
    if any(word in low for word in (
        "experiment", "result", "evaluation", "benchmark", "dataset", "comparison",
        "empirical", "ablation"
    )):
        return "experiment"
    if any(word in low for word in (
        "discussion", "conclusion", "limitation", "future work", "summary"
    )):
        return "discussion"
    if "reference" in low:
        return "references"
    return "other"


def read_order(records: list[BlockRecord], page_width: float) -> list[BlockRecord]:
    """Order block records in human reading order (column-major for 2-col PDFs).

    Full-width blocks come first (top-to-bottom), then each detected column
    top-to-bottom, left column before right. Falls back to y-then-x ordering.
    """
    full = [record for record in records if record[2] - record[0] >= 0.6 * page_width]
    narrow = [record for record in records if record[2] - record[0] < 0.6 * page_width]

    def by_y(items: list[BlockRecord]) -> list[BlockRecord]:
        return sorted(items, key=lambda record: (round(record[1], 1), record[0]))

    left = [record for record in narrow if (record[0] + record[2]) / 2 <= page_width * 0.5]
    right = [record for record in narrow if (record[0] + record[2]) / 2 > page_width * 0.5]
    if left and right and len(narrow) >= 4:
        return by_y(full) + by_y(left) + by_y(right)
    return by_y(records)


def classify_block_roles(
    records: list[BlockRecord], page_height: float, median_size: float
) -> list[tuple[BlockRecord, str]]:
    """Assign a typographic role to each block record.

    Roles: page_number / header / footer / heading / caption / body.
    """
    labelled: list[tuple[BlockRecord, str]] = []
    for x0, y0, x1, y1, text, size in records:
        stripped = " ".join(text.split())
        if not stripped:
            continue
        margin_top = y1 <= page_height * 0.09
        margin_bottom = y0 >= page_height * 0.91
        if (margin_top or margin_bottom) and _is_page_number(stripped):
            labelled.append(((x0, y0, x1, y1, text, size), "page_number"))
            continue
        if (margin_top or margin_bottom) and len(stripped) <= 220 and len(stripped.split()) <= 30:
            labelled.append(((x0, y0, x1, y1, text, size), "header" if margin_top else "footer"))
            continue
        if _is_heading(stripped, size, median_size):
            labelled.append(((x0, y0, x1, y1, text, size), "heading"))
            continue
        if _is_caption(stripped):
            labelled.append(((x0, y0, x1, y1, text, size), "caption"))
            continue
        labelled.append(((x0, y0, x1, y1, text, size), "body"))
    return labelled


def extract_block_records(page_dict: dict) -> list[BlockRecord]:
    """Pull (x0, y0, x1, y1, text, max_font_size) records from a page 'dict'."""
    records: list[BlockRecord] = []
    for block in page_dict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        lines = block.get("lines", [])
        parts: list[str] = []
        max_size = 0.0
        for line in lines:
            spans = line.get("spans", [])
            line_text = "".join(str(span.get("text", "")) for span in spans)
            for span in spans:
                size = float(span.get("size", 0) or 0)
                max_size = max(max_size, size)
            text = " ".join(line_text.split())
            if text:
                parts.append(text)
        text = "\n".join(parts)
        if not text.strip():
            continue
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        records.append((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]),
                        text, max_size))
    return records


def is_reference_page(parsed: ParsedDocument, page_number: int) -> bool:
    """True when the page falls entirely inside a detected references section."""
    for section in parsed.sections:
        if section.type == "references" and section.start_page <= page_number <= section.end_page:
            return True
    return False


def analysis_pages(parsed: ParsedDocument) -> list[DocumentPage]:
    """Pages whose text may be turned into evidence chunks (references skipped)."""
    return [page for page in parsed.pages if not is_reference_page(parsed, page.page_number)]


def _collect_mention_hits(pages: list[DocumentPage]) -> list[tuple[str, str, int, str]]:
    """Find prose references to figures/tables.

    Returns (kind, key, page_number, snippet) tuples. Figure keys support
    sub-figure letters, supplementary numbers and compound references.
    """
    hits: list[tuple[str, str, int, str]] = []
    for page in pages:
        for match in FIGURE_REF_RE.finditer(page.text):
            start, token = match.start(), match.group(1)
            tokens = [_strip_token_marker(token)]
            cursor = match.end()
            # Compound references: "Figs. 3 and 4" / "3, 4, and 5"
            while cursor < min(len(page.text), match.end() + MENTION_COMPOUND_WINDOW):
                continuation = FIGURE_CONTINUATION_RE.match(page.text, cursor)
                if continuation is None:
                    break
                tokens.append(_strip_token_marker(continuation.group(1)))
                cursor = continuation.end()
            for token in tokens:
                hits.append((
                    "figure", token.lower(), page.page_number,
                    _mention_snippet(page.text, start),
                ))
        for match in TABLE_REF_RE.finditer(page.text):
            hits.append((
                "table", match.group(1).lower(), page.page_number,
                _mention_snippet(page.text, match.start()),
            ))
    return hits


def _strip_token_marker(token: str) -> str:
    """Normalize a raw reference token: '3(a)' -> '3a', '3' -> '3'."""
    return re.sub(r"[()]", "", token).strip()


def _assign_visual_mentions(
    figures: list[DocumentFigure],
    tables: list[TableCandidate],
    hits: list[tuple[str, str, int, str]],
) -> None:
    """Attach up to MAX_MENTIONS_PER_VISUAL prose snippets to each figure/table.

    Sub-figure keys ('3a') fall back to the parent figure ('3') when no separate
    sub-figure label exists. A snippet that is the visual's own caption is
    skipped.
    """
    buckets: dict[str, list[tuple[str, object]]] = {"figure": [], "table": []}
    for figure in figures:
        key = _visual_key(figure.label)
        if key:
            buckets["figure"].append((key, figure))
    for table in tables:
        key = _visual_key(table.label)
        if key:
            buckets["table"].append((key, table))

    def matches(visual_key: str | None, hit_key: str) -> bool:
        if visual_key == hit_key:
            return True
        return hit_key != _core_key(hit_key) and visual_key == _core_key(hit_key)

    for kind, key, page_number, snippet in hits:
        for visual_key, visual in buckets[kind]:
            if not matches(visual_key, key):
                continue
            caption = getattr(visual, "caption", None)
            if caption and " ".join(snippet.split()) == " ".join(caption.split()):
                continue  # the hit is the visual's own caption text
            mentions = visual.mentions
            if len(mentions) >= MAX_MENTIONS_PER_VISUAL:
                continue
            if any(item.sentence == snippet for item in mentions):
                continue
            mentions.append(FigureMention(page_number=page_number, sentence=snippet))
            break


def unmatched_figure_references(
    pages: list[DocumentPage], figures: list[DocumentFigure]
) -> list[tuple[str, int]]:
    """Figure labels referenced by prose but never localized by the parser.

    Returns (label, first mentioning page) pairs, e.g. ("Fig. 7", 3). These are
    the P2-a repair requests: the deterministic parser missed the region, so a
    later stage can re-read the page with vision.
    """
    keys = {key for figure in figures if (key := _visual_key(figure.label)) is not None}
    labels: dict[str, int] = {}
    for kind, key, page_number, _ in _collect_mention_hits(pages):
        if kind != "figure":
            continue
        if key in keys or _core_key(key) in keys:
            continue  # already localized (exact label or a parent figure)
        labels.setdefault(f"Fig. {key}", page_number)
    return sorted(labels.items(), key=lambda item: item[1])


class PDFParser:
    def __init__(
        self, workspace: WorkspaceManager, *, render_dpi: int = 144, ocr_languages: str = "eng"
    ) -> None:
        self.workspace = workspace
        self.render_dpi = render_dpi
        self.ocr_languages = ocr_languages

    def parse(self, project_id: str, document_id: str) -> ParsedDocument:
        entry = self.workspace.get_document(project_id, document_id)
        cached = self._cached_parse(project_id, document_id, entry.sha256)
        if cached is not None:
            return cached
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
                    page_records, page_headings = self._page_view(page, page_number)
                    screenshot = self._render_page(project_id, document_id, page, page_number)
                    text = "\n".join(
                        record[4] for record, role in page_records
                        if role not in {"header", "footer", "page_number"}
                    )
                    pages.append(
                        DocumentPage(
                            document_id=document_id,
                            page_number=page_number,
                            text=text,
                            width=page.rect.width,
                            height=page.rect.height,
                            rotation=page.rotation,
                            screenshot_path=screenshot,
                            blocks=[
                                DocumentBlock(index=index, role=role, text=record[4],
                                              bbox=BoundingBox(x0=record[0], y0=record[1],
                                                               x1=record[2], y1=record[3]))
                                for index, (record, role) in enumerate(page_records)
                            ],
                        )
                    )
                    headings.extend(page_headings)
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
            _assign_visual_mentions(figures, tables, _collect_mention_hits(pages))
            # P2-a repair diagnostics: prose references the parser could not
            # localize, and text-light pages that still contain figures.
            for label, page_number in unmatched_figure_references(pages, figures)[:6]:
                warnings.append(f"正文引用 {label} 未匹配到图表区域（第 {page_number} 页）")
            for page in pages:
                if len(page.text.strip()) < 40 and any(
                    figure.page_number == page.page_number for figure in figures
                ):
                    warnings.append(f"第 {page.page_number} 页文本极少但含图表，可能需要视觉阅读")
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
        marker = self.workspace.resolve_safe_path(
            project_id, f"parsed/{document_id}/.source_sha"
        )
        marker.write_text(entry.sha256, encoding="utf-8")
        return parsed

    def _cached_parse(self, project_id: str, document_id: str, sha256: str):
        """Reuse a prior parse when the source PDF content is unchanged."""
        try:
            json_path = self.workspace.resolve_safe_path(
                project_id, f"parsed/{document_id}/document.json"
            )
            marker_path = self.workspace.resolve_safe_path(
                project_id, f"parsed/{document_id}/.source_sha"
            )
            if (json_path.is_file() and marker_path.is_file()
                    and marker_path.read_text(encoding="utf-8") == sha256):
                return ParsedDocument.model_validate_json(json_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("parse cache miss for %s", document_id, exc_info=True)
        return None

    def _page_view(self, page: pymupdf.Page, page_number: int):
        """Return (ordered, role-labelled records, headings) for one page."""
        page_dict = page.get_text("dict")
        records = extract_block_records(page_dict)
        text = "\n".join(record[4] for record in records)
        if len(text.strip()) < 40:
            try:
                text_page = page.get_textpage_ocr(
                    language=self.ocr_languages, dpi=self.render_dpi, full=False
                )
                records = extract_block_records(page.get_text("dict", textpage=text_page))
            except Exception as exc:  # noqa: BLE001 - vision handles scan fallback
                logger.debug("OCR unavailable for page %s: %s", page_number, exc)
        records = read_order(records, page.rect.width)
        sizes = sorted(record[5] for record in records)
        median = statistics.median(sizes) if sizes else 0.0
        labelled = classify_block_roles(records, page.rect.height, median)
        headings: list[tuple[str, int, float]] = []
        for (_, _, _, _, text_block, size), role in labelled:
            if role == "heading":
                stripped = " ".join(text_block.split())[:500]
                confidence = min(1.0, 0.7 + max(size - median, 0) / 20)
                headings.append((stripped, page_number, confidence))
        return labelled, headings

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
                    type=_section_type_for_title(title),
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
