from app.agents.paper_analysis import (
    SPECIALISTS,
    PaperAnalyst,
    _chunk_text,
    _useful_section,
)
from app.schemas import AnalysisClaim, EvidenceNode, PaperAnalysis


def _evidence(identifier: str, page: int, text: str, *, kind: str = "text") -> EvidenceNode:
    common = {
        "evidence_id": identifier,
        "project_id": "project",
        "paper_id": "paper",
        "document_id": "document",
        "evidence_type": kind,
        "claim": text if kind != "text" else "全文证据块",
        "confidence": 1,
        "page_number": page,
        "source_path": "source.png",
        "source_hash": "a" * 64,
        "created_at": "2026-09-03T00:00:00Z",
    }
    if kind == "text":
        common.update({"excerpt": text, "span_start": 0, "span_end": len(text)})
    else:
        common.update({"label": f"Figure {page}", "excerpt": "caption"})
    return EvidenceNode.model_validate(common)


def test_complete_page_chunking_retains_the_end_and_bounds_each_chunk() -> None:
    source = ("方法步骤与实验结果。" * 240) + "最终结论不可丢失"

    chunks = _chunk_text(source)

    assert len(chunks) > 2
    assert all(1 <= len(chunk) <= 950 for chunk in chunks)
    assert chunks[-1].endswith("最终结论不可丢失")


def test_section_filter_rejects_formula_headings() -> None:
    assert _useful_section("3 实验结果与分析") == "3 实验结果与分析"
    assert _useful_section("= arg min ( x + y )") is None


def test_dimension_selection_prefers_relevant_late_experiment_evidence() -> None:
    nodes = [
        _evidence("intro", 1, "研究背景和问题"),
        _evidence("method", 3, "算法方法和处理流程"),
        _evidence("result", 7, "实验结果 数据集 指标 对比 精度"),
    ]
    experiment = next(spec for spec in SPECIALISTS if spec.key == "experiment")

    selected = PaperAnalyst._select_evidence(experiment, nodes, [], "海洋涡旋")

    assert selected[0].evidence_id == "result"


def test_quality_gate_rejects_short_or_incomplete_reports() -> None:
    supported = AnalysisClaim(value="有证据支持的详细分析。" * 8, kind="supported", evidence_ids=["e"])
    inference = AnalysisClaim(value="基于证据边界的局限分析。" * 8, kind="inference")
    report = PaperAnalysis(
        paper_id="paper",
        title="title",
        overview=AnalysisClaim(value="完整概述。" * 25, kind="supported", evidence_ids=["e"]),
        core_problem=[supported, supported.model_copy()],
        methods=[supported, supported.model_copy()],
        mechanisms=[supported, supported.model_copy()],
        experimental_setup=[supported, supported.model_copy()],
        main_results=[supported, supported.model_copy()],
        limitations=[inference, inference.model_copy()],
        relevance_to_topic=[supported, supported.model_copy()],
    )

    assert PaperAnalyst._quality_issues(report) == []
    report.overview = None
    report.methods = [AnalysisClaim(value="太短", kind="inference")]
    issues = PaperAnalyst._quality_issues(report)
    assert "综合概述不足 120 字" in issues
    assert "方法内容不足" in issues
