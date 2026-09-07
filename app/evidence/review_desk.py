"""Decision desk (lean HITL plan): ranking, impact echo and queue assembly.

Pure functions only - no database, no UI, no tokens. Callers (workspace
service / routes / tests) feed in already-loaded report + evidence models and
receive plain dataclasses they can serialize.

Semantics
---------
- ``evidence_reviews`` rows are only written once someone (human or an
  automatic pass) decided an evidence; **no row means unreviewed**.
- Every ``kind=supported`` claim in the stored analysis report cites
  ``evidence_ids``; that gives us, per evidence: how many conclusions rely on
  it and whether any of them lives in the cross-paper comparison.
- "Excluding" an evidence only *really* changes the report after regeneration
  (the regenerated analysis drops it from the allowed pool). The impact echo
  below therefore predicts that regeneration: a supported claim whose
  ``evidence_ids`` is exactly this one will downgrade to inference; a claim
  with other sources stays supported.
"""

from dataclasses import dataclass
from typing import Literal

from app.schemas import AnalysisClaim, AnalysisReport, EvidenceNode

#: Claim fields a PaperAnalysis stores (``overview`` is a single claim, the
#: rest are lists of claims).
PAPER_CLAIM_FIELDS: tuple[str, ...] = (
    "core_problem", "methods", "mechanisms", "experimental_setup",
    "main_results", "limitations", "relevance_to_topic",
)

#: Claim fields a ComparisonDraft stores (``overview`` included in the walk).
COMPARISON_CLAIM_FIELDS: tuple[str, ...] = (
    "commonalities", "differences", "complementarities", "applicability",
)

#: Human-facing section labels shared by the desk API and UI.
FIELD_LABELS: dict[str, str] = {
    "core_problem": "核心问题",
    "methods": "方法流程",
    "mechanisms": "作用机制",
    "experimental_setup": "实验设置",
    "main_results": "主要结果",
    "limitations": "局限",
    "relevance_to_topic": "与课题的关系",
    "overview": "概述",
    "commonalities": "共同点",
    "differences": "差异",
    "complementarities": "互补性",
    "applicability": "适用条件",
}

COMPARISON_PAPER_TITLE = "两篇论文对比"

#: Priority weights (visible knob for future tuning; kept as constants for now).
_CITED_NORM = 5.0
_W_CITED = 0.45
_W_COMPARISON = 0.25
_W_DOUBT = 0.20
_W_CONFIDENCE = 0.10


@dataclass(frozen=True)
class CitingClaim:
    """One supported conclusion that cites an evidence id."""

    scope: Literal["paper", "comparison"]
    paper_title: str
    field: str
    value: str
    kind: str
    evidence_count: int

    @property
    def section_label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field)

    @property
    def will_downgrade(self) -> bool:
        """True when this claim's only source is the evidence under review."""
        return self.evidence_count == 1


@dataclass(frozen=True)
class ReviewInfo:
    status: str | None = None
    source: str | None = None
    note: str | None = None

    @property
    def decided(self) -> bool:
        return self.status is not None

    @property
    def human_decided(self) -> bool:
        return self.decided and self.source == "human"

    @property
    def doubted(self) -> bool:
        return self.status == "doubted"

    @property
    def excluded(self) -> bool:
        return self.status == "excluded"


@dataclass(frozen=True)
class ImpactPreview:
    cited_total: int
    downgrade: tuple[CitingClaim, ...]
    retained: tuple[CitingClaim, ...]
    supports_comparison: bool

    @property
    def downgrade_count(self) -> int:
        return len(self.downgrade)

    @property
    def retained_count(self) -> int:
        return len(self.retained)


@dataclass(frozen=True)
class DeskRow:
    """One actionable queue row (still awaiting or worth a human decision)."""

    evidence: EvidenceNode
    review: ReviewInfo
    citing: tuple[CitingClaim, ...]
    segments: tuple[str, ...]

    @property
    def evidence_id(self) -> str:
        return self.evidence.evidence_id

    @property
    def cited_by(self) -> int:
        return len(self.citing)

    @property
    def supports_comparison(self) -> bool:
        return any(claim.scope == "comparison" for claim in self.citing)

    @property
    def priority(self) -> float:
        return priority_score(
            cited_by=self.cited_by,
            supports_comparison=self.supports_comparison,
            doubted=self.review.doubted,
            confidence=self.evidence.confidence,
        )


@dataclass(frozen=True)
class QueueResult:
    rows: tuple[DeskRow, ...]
    stats: dict[str, int]


def priority_score(
    *,
    cited_by: int,
    supports_comparison: bool,
    doubted: bool,
    confidence: float,
) -> float:
    """Decision value of reviewing this evidence first (0..1)."""
    return (
        _W_CITED * min(cited_by, _CITED_NORM) / _CITED_NORM
        + _W_COMPARISON * (1.0 if supports_comparison else 0.0)
        + _W_DOUBT * (1.0 if doubted else 0.0)
        + _W_CONFIDENCE * (1.0 - confidence)
    )


def _as_claims(value) -> list[AnalysisClaim]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, AnalysisClaim):
        return [value]
    return []


def citation_index(report: AnalysisReport) -> dict[str, list[CitingClaim]]:
    """evidence_id -> supported conclusions citing it (paper + comparison)."""
    index: dict[str, list[CitingClaim]] = {}

    def add(paper_title: str, scope: str, field: str, claims: list[AnalysisClaim]) -> None:
        for claim in claims:
            if claim.kind != "supported" or not claim.evidence_ids:
                continue
            ref = CitingClaim(
                scope=scope,  # type: ignore[arg-type]
                paper_title=paper_title, field=field,
                value=claim.value, kind=claim.kind,
                evidence_count=len(claim.evidence_ids),
            )
            for evidence_id in claim.evidence_ids:
                index.setdefault(evidence_id, []).append(ref)

    for paper in report.papers:
        for field in (*PAPER_CLAIM_FIELDS, "overview"):
            add(paper.title, "paper", field, _as_claims(getattr(paper, field, None)))
    comparison = report.comparison
    if comparison is not None:
        for field in (*COMPARISON_CLAIM_FIELDS, "overview"):
            add(
                COMPARISON_PAPER_TITLE, "comparison", field,
                _as_claims(getattr(comparison, field, None)),
            )
    return index


def citing_claims(
    index: dict[str, list[CitingClaim]], evidence_id: str
) -> tuple[CitingClaim, ...]:
    return tuple(index.get(evidence_id, ()))


def impact_preview(
    index: dict[str, list[CitingClaim]], evidence_id: str
) -> ImpactPreview:
    claims = citing_claims(index, evidence_id)
    return ImpactPreview(
        cited_total=len(claims),
        downgrade=tuple(claim for claim in claims if claim.will_downgrade),
        retained=tuple(claim for claim in claims if not claim.will_downgrade),
        supports_comparison=any(claim.scope == "comparison" for claim in claims),
    )


def review_info(review_row: dict | None) -> ReviewInfo:
    if not review_row:
        return ReviewInfo()
    return ReviewInfo(
        status=review_row.get("status"),
        source=review_row.get("source"),
        note=review_row.get("note"),
    )


def build_queue(
    nodes: list[EvidenceNode],
    review_rows: list[dict],
    index: dict[str, list[CitingClaim]],
) -> QueueResult:
    """Assemble the desk queue with segments and ordering.

    Segments
    --------
    - ``high_risk``: automatic/human doubt that backs at least one conclusion.
    - ``high_impact``: auto-confirmed or unreviewed figure/table evidence that
      backs >= 2 conclusions - cheap high-value confirmation candidates.
    - ``normal``: everything else worth reviewing (other cited evidence,
      unreviewed visuals, doubts with no conclusions behind them).
    Rows in one segment are sorted by decision value (priority) descending.
    Low-value unreviewed prose chunks that no conclusion cites never enter the
    queue, so the human is not paid in mouse clicks on trivia.
    """
    reviews: dict[str, dict] = {
        row["evidence_id"]: row for row in review_rows
    }
    rows: list[DeskRow] = []
    excluded = 0
    for node in nodes:
        review = review_info(reviews.get(node.evidence_id))
        if review.excluded:
            excluded += 1
            continue
        if review.human_decided and not review.doubted:
            continue
        citing = citing_claims(index, node.evidence_id)
        if not citing and node.evidence_type == "text":
            continue  # no conclusion depends on this prose chunk - skip it
        segments: list[str] = []
        if review.doubted and citing:
            segments.append("high_risk")
        elif (
            not review.human_decided
            and len(citing) >= 2
            and node.evidence_type in {"figure", "table"}
        ):
            segments.append("high_impact")
        if not segments:
            segments.append("normal")
        rows.append(DeskRow(
            evidence=node, review=review, citing=tuple(citing),
            segments=tuple(segments),
        ))
    rows.sort(key=lambda row: row.priority, reverse=True)
    stats = {
        "high_risk": sum("high_risk" in row.segments for row in rows),
        "high_impact": sum("high_impact" in row.segments for row in rows),
        "actionable": len(rows),
        "excluded": excluded,
    }
    return QueueResult(rows=tuple(rows), stats=stats)


def filter_rows(rows: tuple[DeskRow, ...], segment: str) -> list[DeskRow]:
    """'priority' = risk + high-impact; 'all' = every queued row."""
    if segment == "all":
        return list(rows)
    return [
        row for row in rows
        if any(tag in {"high_risk", "high_impact"} for tag in row.segments)
    ]
