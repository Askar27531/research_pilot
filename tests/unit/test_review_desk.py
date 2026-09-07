"""Pure decision-desk logic: citation index, impact echo, ranking and queue."""

from app.evidence.review_desk import (
    build_queue,
    citation_index,
    filter_rows,
    impact_preview,
    priority_score,
)
from app.schemas import (
    AnalysisClaim,
    AnalysisReport,
    ComparisonDraft,
    EvidenceNode,
    PaperAnalysis,
)

SHA = "0" * 64


def _figure(evidence_id: str, confidence: float = 0.8, label: str = "Figure 1") -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id, project_id="p", paper_id="paper-a",
        document_id="doc-a", evidence_type="figure", claim=f"{label} 显示了实验趋势",
        confidence=confidence, page_number=3, label=label,
        source_path="fig.png", source_hash=SHA, created_at="2026-01-01T00:00:00Z",
    )


def _text(evidence_id: str, confidence: float = 1.0) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id, project_id="p", paper_id="paper-a",
        document_id="doc-a", evidence_type="text", claim="全文证据块（第 1 页）",
        confidence=confidence, page_number=1, excerpt="正文中的关键句子。",
        span_start=0, span_end=10, source_path="pages/1.txt", source_hash=SHA,
        created_at="2026-01-01T00:00:00Z",
    )


def _supported(value: str, *evidence_ids: str) -> AnalysisClaim:
    return AnalysisClaim(value=value, kind="supported", evidence_ids=list(evidence_ids))


def _report() -> AnalysisReport:
    paper = PaperAnalysis(
        paper_id="paper-a", title="论文 A",
        core_problem=[_supported("论文 A 的核心结论 1", "e1")],
        methods=[_supported("论文 A 的方法依赖图 1", "e1", "e2"),
                 _supported("论文 A 的方法还依赖图 2", "e2", "e3")],
        mechanisms=[], experimental_setup=[], main_results=[], limitations=[],
        relevance_to_topic=[], overview=None,
    )
    second = PaperAnalysis(
        paper_id="paper-b", title="论文 B",
        core_problem=[_supported("论文 B 的核心结论 2", "e2")],
        methods=[], mechanisms=[], experimental_setup=[], main_results=[],
        limitations=[], relevance_to_topic=[], overview=None,
    )
    comparison = ComparisonDraft(
        commonalities=[_supported("两篇论文都依赖图 2 的结果", "e2")],
        differences=[], complementarities=[], applicability=[], overview=None,
    )
    return AnalysisReport(
        project_id="p", search_revision=1, papers=[paper, second],
        comparison=comparison, created_at="2026-01-02T00:00:00Z",
    )


def test_citation_index_counts_paper_and_comparison_claims() -> None:
    index = citation_index(_report())

    assert len(index["e1"]) == 2  # core conclusion + method claim
    assert len(index["e2"]) == 4  # two method claims + paper B + comparison
    assert len(index["e3"]) == 1
    assert any(claim.scope == "comparison" for claim in index["e2"])
    # A claim that cites several evidence ids carries its full evidence count.
    method_claim = next(claim for claim in index["e3"] if claim.evidence_count == 2)
    assert method_claim.evidence_count == 2


def test_impact_preview_distinguishes_downgrade_from_retained() -> None:
    index = citation_index(_report())

    # e3: cited once, by a claim that also has another source -> retained.
    impact = impact_preview(index, "e3")
    assert impact.cited_total == 1
    assert impact.retained_count == 1
    assert impact.downgrade_count == 0

    # e2: cited four times; two of its claims are e2-only (paper B core and the
    # comparison commonality) and would downgrade if e2 were excluded.
    impact = impact_preview(index, "e2")
    assert impact.cited_total == 4
    assert impact.supports_comparison is True
    assert impact.downgrade_count == 2
    assert all(claim.will_downgrade for claim in impact.downgrade)
    assert all(claim.evidence_count == 1 for claim in impact.downgrade)


def test_build_queue_segments_priority_and_resolution() -> None:
    report = _report()
    index = citation_index(report)
    nodes = [
        _figure("e1", confidence=0.7),  # doubted + cited        -> high_risk
        _figure("e2", confidence=0.9),  # auto confirmed         -> high_impact
        _figure("e3", confidence=0.5),  # unreviewed, cited 1x   -> normal
        _figure("e4"),                  # unreviewed, uncited    -> normal
        _figure("e5"),                  # human excluded         -> dropped + counted
        _text("t1"),                    # unreviewed prose, uncited -> dropped
    ]
    reviews = [
        {"evidence_id": "e1", "status": "doubted", "note": "自动复核：可疑",
         "source": "auto_visual_verifier", "updated_at": "2026-01-01T00:00:00Z"},
        {"evidence_id": "e2", "status": "confirmed", "note": "自动复核：支持",
         "source": "auto_visual_verifier", "updated_at": "2026-01-01T00:00:00Z"},
        {"evidence_id": "e5", "status": "excluded", "note": "人工排除",
         "source": "human", "updated_at": "2026-01-01T00:00:00Z"},
    ]

    result = build_queue(nodes, reviews, index)
    assert result.stats == {"high_risk": 1, "high_impact": 1,
                            "actionable": 4, "excluded": 1}
    rows_by_id = {row.evidence_id: row for row in result.rows}
    assert {"e5", "t1"} & set(rows_by_id) == set()

    # Doubt and comparison support both weigh in; the two priority rows must
    # outrank the plain rows, and e2 (4 citations + comparison) leads e1.
    order = [row.evidence_id for row in result.rows]
    assert order[0] == "e2"
    assert {order[0], order[1]} == {"e1", "e2"}
    assert order.index("e2") < order.index("e3")
    assert rows_by_id["e1"].segments == ("high_risk",)
    assert rows_by_id["e2"].segments == ("high_impact",)

    # A human confirmation never re-enters the queue afterwards.
    nodes_with_human = nodes + [_figure("e9")]
    human_reviews = reviews + [{
        "evidence_id": "e9", "status": "confirmed", "note": "人工确认",
        "source": "human", "updated_at": "2026-01-01T00:00:00Z",
    }]
    second = build_queue(nodes_with_human, human_reviews, index)
    assert "e9" not in {row.evidence_id for row in second.rows}


def test_filter_rows_priority_and_all() -> None:
    index = citation_index(_report())
    nodes = [
        _figure("e1", confidence=0.7),
        _figure("e2", confidence=0.9),
        _figure("e4"),
    ]
    reviews = [
        {"evidence_id": "e1", "status": "doubted", "note": "存疑",
         "source": "auto_visual_verifier", "updated_at": "2026-01-01T00:00:00Z"},
        {"evidence_id": "e2", "status": "confirmed", "note": "支持",
         "source": "auto_visual_verifier", "updated_at": "2026-01-01T00:00:00Z"},
    ]
    result = build_queue(nodes, reviews, index)

    priority_ids = {row.evidence_id for row in filter_rows(result.rows, "priority")}
    assert priority_ids == {"e1", "e2"}  # e4 is an uncited visual -> normal only
    assert {row.evidence_id for row in filter_rows(result.rows, "all")} == {
        "e1", "e2", "e4",
    }


def test_priority_score_bounds_and_monotonicity() -> None:
    plain = priority_score(
        cited_by=1, supports_comparison=False, doubted=False, confidence=0.9
    )
    doubted = priority_score(
        cited_by=1, supports_comparison=False, doubted=True, confidence=0.9
    )
    cited = priority_score(
        cited_by=6, supports_comparison=False, doubted=False, confidence=0.9
    )
    comparison = priority_score(
        cited_by=1, supports_comparison=True, doubted=False, confidence=0.9
    )
    assert doubted > plain
    assert cited > plain
    assert comparison > plain
    for score in (plain, doubted, cited, comparison):
        assert 0.0 <= score <= 1.0
