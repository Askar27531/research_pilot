from datetime import UTC, datetime

import aiosqlite
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.experiments import build_experiment_graph
from app.schemas import (
    EvaluationPlan,
    Experiment,
    ExperimentProposal,
    Hypothesis,
)


class FixedBuilder:
    async def build(self, project_id, objective, evidence_ids):
        now = datetime.now(UTC).isoformat()
        return ExperimentProposal(
            proposal_id="proposal-1",
            project_id=project_id,
            version=1,
            status="pending",
            created_at=now,
            updated_at=now,
            hypotheses=[
                Hypothesis(
                    hypothesis_id="h1",
                    statement="Adapters improve registration",
                    evidence_ids=evidence_ids,
                    confidence=0.8,
                )
            ],
            experiments=[
                Experiment(
                    experiment_id="x1",
                    title="Adapter test",
                    baseline="No adapter",
                    modification="Add adapter",
                    controls=["data"],
                    metrics=["error"],
                    success_criterion="Error decreases",
                    failure_criterion="Error does not decrease",
                    evidence_ids=evidence_ids,
                )
            ],
            evaluation=EvaluationPlan(metrics=["error"], reporting=["mean"]),
        )


@pytest.mark.asyncio
async def test_human_approval_resumes_after_sqlite_connection_restart(tmp_path) -> None:
    path = tmp_path / "checkpoint.db"
    config = {"configurable": {"thread_id": "experiment:project-1"}}
    serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
    first_connection = await aiosqlite.connect(path)
    first_saver = AsyncSqliteSaver(first_connection, serde=serializer)
    await first_saver.setup()
    first_graph = build_experiment_graph(FixedBuilder(), first_saver)

    interrupted = await first_graph.ainvoke(
        {
            "project_id": "project-1",
            "objective": "Test adapters",
            "evidence_ids": ["e1"],
            "proposal": None,
        },
        config=config,
    )
    assert interrupted["proposal"]["status"] == "pending"
    assert interrupted["__interrupt__"]
    await first_connection.close()

    second_connection = await aiosqlite.connect(path)
    second_saver = AsyncSqliteSaver(second_connection, serde=serializer)
    second_graph = build_experiment_graph(FixedBuilder(), second_saver)
    resumed = await second_graph.ainvoke(
        Command(resume={"action": "accept", "version": 1}), config=config
    )
    await second_connection.close()

    assert resumed["proposal"]["status"] == "accepted"
    assert resumed["proposal"]["version"] == 2
