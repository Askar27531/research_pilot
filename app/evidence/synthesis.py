import csv
from io import StringIO

from app.db import EvidenceRepository
from app.documents import DocumentService
from app.schemas import (
    CompactedContext,
    ComparisonCell,
    ComparisonRow,
    CrossPaperComparison,
    PaperSummary,
)

SUMMARY_COLUMNS = ["method", "datasets", "metrics", "contributions", "limitations"]


class ContextCompactor:
    def __init__(self, documents: DocumentService, evidence: EvidenceRepository) -> None:
        self.documents = documents
        self.evidence = evidence

    async def compact(self, summary: PaperSummary, document_id: str) -> CompactedContext:
        parsed = self.documents.get_structure(summary.project_id, document_id)
        nodes = await self.evidence.list_for_paper(summary.project_id, summary.paper_id)
        source_characters = sum(len(page.text) for page in parsed.pages)
        compacted_characters = len(summary.model_dump_json()) + sum(
            len(node.excerpt or "") for node in nodes
        )
        reduction = (
            max(0.0, min(1.0, 1 - compacted_characters / source_characters))
            if source_characters
            else 0.0
        )
        return CompactedContext(
            project_id=summary.project_id,
            paper_id=summary.paper_id,
            summary=summary,
            evidence_index=nodes,
            source_characters=source_characters,
            compacted_characters=compacted_characters,
            reduction_ratio=reduction,
        )


class CrossPaperSynthesizer:
    def build(self, project_id: str, summaries: list[PaperSummary]) -> CrossPaperComparison:
        rows: list[ComparisonRow] = []
        for summary in summaries:
            cells: dict[str, ComparisonCell] = {}
            for column in SUMMARY_COLUMNS:
                claims = getattr(summary, column)
                if not claims:
                    cells[column] = ComparisonCell()
                    continue
                supported = [claim for claim in claims if claim.kind == "supported"]
                selected = supported or claims
                kind = "supported" if supported else "inference"
                cells[column] = ComparisonCell(
                    value="; ".join(claim.value for claim in selected),
                    kind=kind,
                    evidence_ids=(
                        list(dict.fromkeys(eid for claim in selected for eid in claim.evidence_ids))
                        if kind == "supported"
                        else []
                    ),
                )
            rows.append(ComparisonRow(paper_id=summary.paper_id, cells=cells))
        return CrossPaperComparison(project_id=project_id, columns=SUMMARY_COLUMNS, rows=rows)

    @staticmethod
    def render_csv(comparison: CrossPaperComparison) -> str:
        stream = StringIO()
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["paper_id", *comparison.columns])
        for row in comparison.rows:
            writer.writerow(
                [row.paper_id, *(row.cells[column].value or "" for column in comparison.columns)]
            )
        return stream.getvalue()

    @staticmethod
    def render_markdown(comparison: CrossPaperComparison) -> str:
        header = ["paper_id", *comparison.columns]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in comparison.rows:
            values = [
                (row.cells[column].value or "").replace("|", "\\|").replace("\n", " ")
                for column in comparison.columns
            ]
            lines.append("| " + " | ".join([row.paper_id, *values]) + " |")
        return "\n".join(lines) + "\n"
