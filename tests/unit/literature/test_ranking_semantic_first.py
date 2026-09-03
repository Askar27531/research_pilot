"""Semantic-first ranking: LLM-judged relevant papers lead by relevance score."""

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.literature.ranking import rank_papers
from app.schemas import (
    PaperMetadata,
    PaperScreeningBatch,
    PaperScreeningDecision,
    ResearchRequest,
)


class _ScreeningProvider:
    """Scores each uncached candidate based on keywords in its title."""

    _scores: ClassVar[dict[str, tuple[bool, int]]] = {
        "semantic top": (True, 95),
        "semantic mid": (True, 60),
        "not relevant": (False, 5),
    }

    async def structured_output(self, messages, response_model):
        content = messages[-1]["content"]
        payload = json.loads(content.split("\nPapers: ", 1)[1])
        decisions = [
            PaperScreeningDecision(
                stable_id=item["stable_id"],
                include=self._scores[item["title"]][0],
                relevance=self._scores[item["title"]][1],
                reason="screened by test provider",
            )
            for item in payload
        ]
        return PaperScreeningBatch(decisions=decisions)


def _paper(title: str, *, abstract: str | None = None) -> PaperMetadata:
    if abstract is None and title != "quantum chemistry review":
        abstract = (
            f"{title} about multi-agent reinforcement learning and wildfire suppression"
        )
    return PaperMetadata(
        stable_id=f"stable-{title.replace(' ', '-')}",
        source_id=title,
        title=title,
        abstract=abstract,
    )


def _request() -> ResearchRequest:
    return ResearchRequest(
        research_question="multi-agent reinforcement learning for wildfire suppression",
        maximum_papers=3,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        ranking_llm_top_n=10,
        ranking_abstract_chars=600,
        ollama_model="test-model",
    )


@pytest.mark.asyncio
async def test_llm_judged_relevant_papers_lead_by_semantic_score() -> None:
    papers = [
        _paper("semantic top"),
        _paper("not relevant"),
        _paper("semantic mid"),
        _paper("quantum chemistry review", abstract=None),  # low lexical, never screened
    ]

    ranked, warnings = await rank_papers(
        papers, _request(), _ScreeningProvider(), settings=_settings()
    )

    assert warnings == []
    assert [item.paper.title for item in ranked] == [
        "semantic top", "semantic mid", "quantum chemistry review",
    ]
    top, mid, tail = ranked
    assert top.llm_score == 0.95 and top.final_score == 0.95
    assert mid.llm_score == 0.6 and mid.final_score == 0.6
    assert tail.llm_score is None and tail.final_score == tail.lexical_score


@pytest.mark.asyncio
async def test_lexical_score_no_longer_outranks_judged_relevance() -> None:
    papers = [
        _paper("semantic mid"),
        _paper("semantic top"),
    ]

    ranked, _ = await rank_papers(
        papers, _request(), _ScreeningProvider(), settings=_settings()
    )

    # Both are screened; ordering is purely semantic.
    assert [item.paper.title for item in ranked] == ["semantic top", "semantic mid"]
