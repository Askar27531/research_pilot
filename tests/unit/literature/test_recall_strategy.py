"""Recall-first behavior of concept filtering and query de-duplication."""

import pytest

from app.literature.filtering import filter_by_required_concepts
from app.literature.query import _deduplicate_query_plan
from app.schemas import PaperMetadata, SearchQuery, SearchQueryPlan


def _paper(title: str, abstract: str | None = None) -> PaperMetadata:
    return PaperMetadata(
        stable_id=f"stable-{hash(title) % 100_000}",
        source_id="synthetic",
        title=title,
        abstract=abstract,
    )


def _groups() -> list[list[str]]:
    return [
        ["multi-agent systems", "multi-agent", "multi agent"],
        ["reinforcement learning", "rl"],
        ["wildfire suppression", "firefighting", "forest fire"],
    ]


class TestRecallFirstConceptFilter:
    def test_paper_matching_one_group_is_kept(self) -> None:
        paper = _paper("Communication-aware MARL", "Reinforcement learning for UAV teams.")
        groups = [["multi-agent", "marl"], ["reinforcement learning"]]

        result = filter_by_required_concepts([paper], groups, [])

        assert result.included == [paper]
        assert result.excluded == []
        assert "reinforcement learning" in result.matched_concepts[paper.stable_id]

    def test_paper_matching_no_group_is_excluded(self) -> None:
        paper = _paper("Wildfire risk zoning", "Satellite imagery analysis.")

        result = filter_by_required_concepts([paper], _groups(), [])

        assert result.included == []
        assert result.excluded == [paper]

    def test_excluded_topic_still_filters_out_paper(self) -> None:
        paper = _paper("RL survey", "Multi-agent reinforcement learning for wildfires.")

        result = filter_by_required_concepts([paper], _groups(), ["survey"])

        assert result.excluded == [paper]

    def test_no_required_groups_keeps_everything(self) -> None:
        paper = _paper("Anything", None)

        result = filter_by_required_concepts([paper], [], [])

        assert result.included == [paper]


def _plan(queries: list[SearchQuery]) -> SearchQueryPlan:
    return SearchQueryPlan(
        topic="topic",
        required_concept_groups=_groups(),
        queries=queries,
    )


def _query(text: str, concepts: list[str]) -> SearchQuery:
    return SearchQuery(query=text, concepts=concepts, purpose="high_precision")


class TestQueryDeduplicationRecall:
    def test_broad_query_is_kept_alongside_strong_ones(self) -> None:
        strong = [
            _query(
                '("multi-agent" AND "reinforcement learning" AND "wildfire suppression")',
                ["multi-agent", "reinforcement learning", "wildfire suppression"],
            ),
            _query(
                '("multi-agent" AND "rl" AND "firefighting")',
                ["multi-agent", "rl", "firefighting"],
            ),
        ]
        broad = _query('("multi-agent" OR "multi agent") AND "reinforcement learning"',
                       ["multi-agent", "reinforcement learning"])

        plan = _deduplicate_query_plan(_plan([*strong, broad]))

        texts = [item.query for item in plan.queries]
        assert broad.query in texts
        assert len(plan.queries) == 3

    def test_near_duplicate_wording_is_collapsed(self) -> None:
        first = _query('("multi-agent" AND "rl" AND "wildfire suppression")',
                       ["multi-agent", "rl", "wildfire suppression"])
        duplicate = _query('("multi-agent" AND "rl" AND "wildfire suppression")',
                           ["multi-agent", "rl", "wildfire suppression"])
        third = _query('("marl" AND "firefighting")', ["marl", "firefighting"])
        fourth = _query('("multi-agent" OR "multi agent") AND "wildfire suppression"',
                        ["multi-agent", "wildfire suppression"])

        plan = _deduplicate_query_plan(_plan([first, duplicate, third, fourth]))

        assert len(plan.queries) == 3

    def test_plan_with_fewer_than_three_usable_queries_raises(self) -> None:
        only_broad = [_query('"reinforcement learning" wildfires', ["reinforcement learning"])]

        with pytest.raises(ValueError):
            _deduplicate_query_plan(_plan(only_broad))
