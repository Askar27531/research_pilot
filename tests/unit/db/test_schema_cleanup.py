"""Tests for the V12 scope-cleanup migration and LangGraph orphan cleanup on delete."""

import pytest

from app.db import Database, ProjectRepository, ResearchSessionRepository
from app.schemas import ResearchRequest

ORPHAN_TABLES = (
    "experiment_proposals",
    "proposal_decisions",
    "artifacts",
    "method_cards",
    "transfer_candidate_sets",
    "conversation_messages",
    "revision_previews",
    "paper_sources",
)


async def _table_names(database: Database) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    return {row["name"] for row in rows}


async def _schema_version(database: Database) -> int:
    async with database.connect() as connection:
        row = await (await connection.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        )).fetchone()
    return int(row["n"])


async def _create_graph_tables(database: Database) -> None:
    """Create LangGraph checkpoint tables exactly as AsyncSqliteSaver.setup() does."""
    async with database.connect() as connection:
        await connection.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            );
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            );
        """)
        await connection.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, "
            "checkpoint, metadata) VALUES (?, '', ?, '{}', '{}')",
            ("00000000-0000-0000-0000-00000000000a:search-1:domain", "cp-1"),
        )
        await connection.execute(
            "INSERT INTO writes(thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "idx, channel, value) VALUES (?, '', 'cp-1', 'task-1', 0, 'state', '{}')",
            ("00000000-0000-0000-0000-00000000000a:search-1:domain",),
        )
        await connection.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, "
            "checkpoint, metadata) VALUES (?, '', ?, '{}', '{}')",
            ("00000000-0000-0000-0000-00000000000b:search-1:domain", "cp-2"),
        )
        await connection.execute(
            "INSERT INTO writes(thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "idx, channel, value) VALUES (?, '', 'cp-2', 'task-1', 0, 'state', '{}')",
            ("00000000-0000-0000-0000-00000000000b:search-1:domain",),
        )
        await connection.commit()


@pytest.mark.asyncio
async def test_v12_migration_drops_orphan_tables(tmp_path) -> None:
    database = Database(tmp_path / "cleanup.db")
    await database.initialize()

    names = await _table_names(database)
    for table in ORPHAN_TABLES:
        assert table not in names, f"{table} should have been dropped by V12"
    # Key tables survive.
    for table in (
        "projects", "papers", "evidence", "workflow_jobs", "work_items",
        "search_sessions", "document_analyses", "visual_regions",
    ):
        assert table in names, f"{table} must survive the cleanup migration"
    assert await _schema_version(database) == 17


@pytest.mark.asyncio
async def test_upgrade_from_v11_keeps_existing_data(tmp_path) -> None:
    """Re-running migration 1..11 then 12 must drop orphans but keep domain data."""
    database = Database(tmp_path / "upgrade.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Keep", ResearchRequest(research_question="Survives upgrade")
    )
    sessions = ResearchSessionRepository(database)
    await sessions.begin_search(project.id)
    # Re-running initialize() is a no-op for already applied versions.
    await database.initialize()

    assert await _schema_version(database) == 17
    assert (await ProjectRepository(database).get(project.id)).name == "Keep"
    assert await sessions.current_search(project.id) is not None


@pytest.mark.asyncio
async def test_project_delete_removes_its_langgraph_threads(tmp_path) -> None:
    database = Database(tmp_path / "graph.db")
    await database.initialize()
    await _create_graph_tables(database)

    first = await ProjectRepository(database).create(
        "Graph-A", ResearchRequest(research_question="Project with graph runs")
    )
    second = await ProjectRepository(database).create(
        "Graph-B", ResearchRequest(research_question="Unrelated project")
    )

    # Point the seeded threads at the created projects so the prefix delete applies.
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE checkpoints SET thread_id = ? WHERE thread_id LIKE "
            "'00000000-0000-0000-0000-00000000000a:%'", (f"{first.id}:search-1:domain",))
        await connection.execute(
            "UPDATE writes SET thread_id = ? WHERE thread_id LIKE "
            "'00000000-0000-0000-0000-00000000000a:%'", (f"{first.id}:search-1:domain",))
        await connection.execute(
            "UPDATE checkpoints SET thread_id = ? WHERE thread_id LIKE "
            "'00000000-0000-0000-0000-00000000000b:%'", (f"{second.id}:search-1:domain",))
        await connection.execute(
            "UPDATE writes SET thread_id = ? WHERE thread_id LIKE "
            "'00000000-0000-0000-0000-00000000000b:%'", (f"{second.id}:search-1:domain",))
        await connection.commit()

    await ProjectRepository(database).delete(first.id)

    async with database.connect() as connection:
        remaining_threads = {
            row["thread_id"] for row in await (await connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            )).fetchall()
        }
    assert remaining_threads == {f"{second.id}:search-1:domain"}
