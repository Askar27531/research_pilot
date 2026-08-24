from collections import Counter

import pytest

from app.agents.literature import TracedLiteratureClient
from app.db import Database, ProjectRepository, TraceRepository, WorkItemRepository
from app.reliability import FailureInjector, InjectedFailure
from app.schemas import ResearchRequest, SearchResult


class CountingLiterature:
    def __init__(self) -> None:
        self.calls = Counter()

    async def search_papers(self, query, year_from=None, year_to=None, limit=20):
        self.calls[query] += 1
        return SearchResult(query=query, papers=[], total_available=0, source_latency_ms=1)


@pytest.mark.asyncio
async def test_fifth_item_failure_resumes_without_repeating_first_four(tmp_path) -> None:
    database = Database(tmp_path / "recovery.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Recovery", ResearchRequest(research_question="Test item recovery")
    )
    upstream = CountingLiterature()
    items = WorkItemRepository(database)
    traces = TraceRepository(database)
    failing = TracedLiteratureClient(
        upstream,
        traces,
        project.id,
        "trace-1",
        items,
        FailureInjector(fail_at=5),
    )
    queries = [f"query-{index}" for index in range(1, 7)]

    with pytest.raises(InjectedFailure):
        for query in queries:
            await failing.search_papers(query)

    resumed = TracedLiteratureClient(upstream, traces, project.id, "trace-1", items)
    for query in queries:
        await resumed.search_papers(query)

    assert all(upstream.calls[query] == 1 for query in queries)
    progress = await items.list_for_project(project.id)
    assert len(progress) == 6
    assert all(item.status == "completed" for item in progress)
    failed_then_recovered = next(item for item in progress if item.attempts == 2)
    assert failed_then_recovered.item_key
    assert (await items.metrics(project.id)).recovered_items == 1
    metrics = await traces.metrics(project.id)
    assert metrics.recovery_count == 1
    assert metrics.failed_events == 1
