"""V15 M3 cost-threshold pause: usage ledger, budget windows and the gate
semantics behind "pause at a safe boundary when the run crosses a budget"."""

import pytest

from app.core.config import Settings
from app.db import Database, ProjectRepository, ResearchDataRepository
from app.schemas import ResearchRequest


async def _table_names(database: Database) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    return {row["name"] for row in rows}


async def _project_columns(database: Database) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(
            "PRAGMA table_info(projects)"
        )).fetchall()
    return {row["name"] for row in rows}


def _tiny_settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "ollama_model": "qwen:budget-test",
        "budget_gate_tokens": 1_000,
        "budget_gate_vision_calls": 2,
        "budget_gate_minutes": 5,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_v15_adds_usage_ledger_and_budget_window(tmp_path) -> None:
    database = Database(tmp_path / "v15.db")
    await database.initialize()

    tables = await _table_names(database)
    assert {"llm_usage", "budget_windows"} <= tables
    columns = await _project_columns(database)
    assert "pause_reason" in columns


@pytest.mark.asyncio
async def test_usage_ledger_totals_roundtrip(tmp_path) -> None:
    database = Database(tmp_path / "usage.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Usage", ResearchRequest(research_question="Meter every call")
    )
    repository = ResearchDataRepository(database)

    await repository.record_usage(
        project.id, phase="document_analysis", kind="text", model="qwen",
        prompt_tokens=100, completion_tokens=40,
    )
    await repository.record_usage(
        project.id, phase="document_analysis", kind="vision", model="qwen-vl",
        prompt_tokens=400, completion_tokens=260,
    )
    await repository.record_usage(
        project.id, phase="research", kind="text", model="qwen",
        prompt_tokens=50, completion_tokens=10,
    )

    totals = await repository.usage_totals(project.id)
    assert totals["tokens"] == 860
    assert totals["calls"] == 3
    assert totals["vision_calls"] == 1
    assert totals["first_at"] is not None


@pytest.mark.asyncio
async def test_gate_ignores_usage_below_thresholds(tmp_path) -> None:
    database = Database(tmp_path / "below.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Below", ResearchRequest(research_question="Small run keeps going")
    )
    repository = ResearchDataRepository(database)
    await repository.record_usage(project.id, kind="text", prompt_tokens=100)

    settings = _tiny_settings()
    assert await repository.budget_pause_reason(project.id, settings) is None
    window = await repository.budget_window(project.id)
    assert window["open"] is False


@pytest.mark.asyncio
async def test_gate_opens_once_then_ack_rebaselines(tmp_path) -> None:
    database = Database(tmp_path / "gate.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Gate", ResearchRequest(research_question="Cross a threshold")
    )
    repository = ResearchDataRepository(database)
    settings = _tiny_settings()

    # 1_000 prompt tokens alone cross the 1_000-token threshold.
    await repository.record_usage(project.id, kind="text", prompt_tokens=1_000)
    reason = await repository.budget_pause_reason(project.id, settings)
    assert reason == "budget_gate"
    window = await repository.budget_window(project.id)
    assert window["open"] is True
    assert window["snapshot"]["tokens"] == 1_000

    # While the gate is open the run must not be asked again at every boundary.
    assert await repository.budget_pause_reason(project.id, settings) is None

    # "Continue" closes the gate and re-baselines to the current totals.
    await repository.ack_budget_gate(project.id)
    window = await repository.budget_window(project.id)
    assert window["open"] is False
    assert window["baseline_tokens"] == 1_000

    # Another threshold-sized block is needed before the next pause.
    await repository.record_usage(project.id, kind="text", prompt_tokens=500)
    assert await repository.budget_pause_reason(project.id, settings) is None
    await repository.record_usage(project.id, kind="text", prompt_tokens=600)
    assert await repository.budget_pause_reason(project.id, settings) == "budget_gate"


@pytest.mark.asyncio
async def test_gate_counts_vision_calls_separately(tmp_path) -> None:
    database = Database(tmp_path / "vision.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Vision", ResearchRequest(research_question="Vision budget")
    )
    repository = ResearchDataRepository(database)
    # Tiny token totals: only the vision-call threshold (2) can trigger.
    settings = _tiny_settings(budget_gate_vision_calls=2)

    await repository.record_usage(project.id, kind="vision", prompt_tokens=1)
    assert await repository.budget_pause_reason(project.id, settings) is None
    await repository.record_usage(project.id, kind="vision", prompt_tokens=1)
    assert await repository.budget_pause_reason(project.id, settings) == "budget_gate"


@pytest.mark.asyncio
async def test_gate_minutes_trigger_from_first_usage(tmp_path) -> None:
    database = Database(tmp_path / "minutes.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Minutes", ResearchRequest(research_question="Wall clock budget")
    )
    repository = ResearchDataRepository(database)
    settings = _tiny_settings(budget_gate_minutes=1)

    # Backdate the ledger so the window has been running for > 1 minute.
    await repository.record_usage(project.id, kind="text", prompt_tokens=1)
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE llm_usage SET created_at='2020-01-01T00:00:00+00:00' "
            "WHERE project_id=?", (project.id,)
        )
        await connection.commit()

    assert await repository.budget_pause_reason(project.id, settings) == "budget_gate"


@pytest.mark.asyncio
async def test_user_pause_wins_and_works_when_gate_disabled(tmp_path) -> None:
    database = Database(tmp_path / "user.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "UserPause", ResearchRequest(research_question="Manual beats automatic")
    )
    repository = ResearchDataRepository(database)
    projects = ProjectRepository(database)
    settings = _tiny_settings(budget_gate_enabled=False)

    await repository.record_usage(project.id, kind="text", prompt_tokens=100_000)
    assert await repository.budget_pause_reason(project.id, settings) is None

    await projects.set_pause_requested(project.id, True)
    assert await repository.budget_pause_reason(project.id, settings) == "user"
    await projects.set_pause_requested(project.id, False)


@pytest.mark.asyncio
async def test_pause_reason_roundtrip_and_cascade_delete(tmp_path) -> None:
    database = Database(tmp_path / "reason.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Reason", ResearchRequest(research_question="Pause reason provenance")
    )
    projects = ProjectRepository(database)
    repository = ResearchDataRepository(database)

    await projects.set_pause_reason(project.id, "budget_gate")
    assert (await projects.get(project.id)).pause_reason == "budget_gate"
    await projects.set_pause_reason(project.id, "user")
    assert (await projects.get(project.id)).pause_reason == "user"

    await repository.record_usage(project.id, kind="text", prompt_tokens=10)
    await projects.delete(project.id)
    totals = await repository.usage_totals(project.id)
    assert totals["calls"] == 0  # FK cascade removed the ledger rows
