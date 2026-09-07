"""CI enforcement of the P2-c parser gold evaluation."""

from evals.parser_gold import evaluate_parser


def test_parser_gold_metrics_all_pass(tmp_path) -> None:
    metrics = evaluate_parser(tmp_path)

    assert metrics["all_pass"], metrics
    # Spot-check the individual gates that matter most for downstream quality.
    assert metrics["figure_label_found"] is True
    assert metrics["figure_page_correct"] is True
    assert metrics["mentions_complete"] is True
    assert metrics["header_excluded"] is True
    assert metrics["references_page_excluded"] is True
    assert metrics["cache_stable"] is True
    assert metrics["page_count"] == 4
