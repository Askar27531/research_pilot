from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import dump_json, load_json, utc_now
from app.schemas import AnalysisReport, PaperAnalysis


class ResearchSessionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def begin_search(self, project_id: str, instruction: str | None = None) -> int:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (await connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 revision FROM search_sessions "
                "WHERE project_id=?", (project_id,),
            )).fetchone()
            revision = row["revision"]
            await connection.execute(
                "INSERT INTO search_sessions(project_id,revision,instruction,status,created_at,updated_at) "
                "VALUES(?,?,?,'queued',?,?)", (project_id, revision, instruction, now, now),
            )
            await connection.execute("UPDATE papers SET selected=0 WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM paper_selections WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM analysis_reports WHERE project_id=?", (project_id,))
            await connection.execute(
                "DELETE FROM selected_paper_analyses WHERE project_id=?", (project_id,)
            )
            await connection.execute("DELETE FROM paper_summaries WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM paper_acquisitions WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM documents WHERE project_id=?", (project_id,))
            await connection.commit()
        return revision

    async def pending_search(self, project_id: str) -> dict:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM search_sessions WHERE project_id=? AND status='queued' "
                "ORDER BY revision DESC LIMIT 1", (project_id,),
            )).fetchone()
        if row is None:
            raise RecordNotFoundError("Pending search session not found")
        return dict(row)

    async def complete_search(
        self, project_id: str, revision: int, plan: dict, paper_ids: list[str]
    ) -> None:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "UPDATE search_sessions SET status='completed',plan_json=?,updated_at=? "
                "WHERE project_id=? AND revision=? AND status='queued'",
                (dump_json(plan), now, project_id, revision),
            )
            if cursor.rowcount != 1:
                raise ProjectConflictError("Search revision is no longer pending")
            await connection.executemany(
                "INSERT OR IGNORE INTO search_result_papers(project_id,search_revision,paper_id) "
                "VALUES(?,?,?)", [(project_id, revision, paper_id) for paper_id in paper_ids],
            )
            await connection.commit()

    async def current_search(self, project_id: str) -> dict | None:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM search_sessions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                (project_id,),
            )).fetchone()
        if row is None:
            return None
        return {**dict(row), "plan": load_json(row["plan_json"])}

    async def current_paper_ids(self, project_id: str, revision: int) -> list[str]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT paper_id FROM search_result_papers WHERE project_id=? AND search_revision=?",
                (project_id, revision),
            )).fetchall()
        return [row["paper_id"] for row in rows]

    async def select(
        self, project_id: str, revision: int, paper_ids: list[str], requirements: str | None
    ) -> None:
        if not 1 <= len(paper_ids) <= 2 or len(set(paper_ids)) != len(paper_ids):
            raise ProjectConflictError("Select one or two distinct papers")
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            latest = await (await connection.execute(
                "SELECT revision,status FROM search_sessions WHERE project_id=? "
                "ORDER BY revision DESC LIMIT 1", (project_id,),
            )).fetchone()
            if latest is None or latest["revision"] != revision or latest["status"] != "completed":
                raise ProjectConflictError("Paper token belongs to an outdated search")
            placeholders = ",".join("?" for _ in paper_ids)
            rows = await (await connection.execute(
                f"SELECT paper_id FROM search_result_papers WHERE project_id=? "
                f"AND search_revision=? AND paper_id IN ({placeholders})",
                (project_id, revision, *paper_ids),
            )).fetchall()
            if {row["paper_id"] for row in rows} != set(paper_ids):
                raise ProjectConflictError("Selected paper is not in the current search")
            await connection.execute("UPDATE papers SET selected=0 WHERE project_id=?", (project_id,))
            await connection.executemany(
                "UPDATE papers SET selected=1 WHERE project_id=? AND id=?",
                [(project_id, paper_id) for paper_id in paper_ids],
            )
            await connection.execute(
                "INSERT INTO paper_selections(project_id,search_revision,paper_ids_json,requirements,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "search_revision=excluded.search_revision,paper_ids_json=excluded.paper_ids_json,"
                "requirements=excluded.requirements,updated_at=excluded.updated_at",
                (project_id, revision, dump_json(paper_ids), requirements, now, now),
            )
            await connection.commit()

    async def selection(self, project_id: str) -> dict | None:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM paper_selections WHERE project_id=?", (project_id,)
            )).fetchone()
        return {**dict(row), "paper_ids": load_json(row["paper_ids_json"])} if row else None

    async def reset_selection(self, project_id: str) -> None:
        """Return to paper selection while retaining the current search results."""
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            current = await (await connection.execute(
                "SELECT status FROM search_sessions WHERE project_id=? "
                "ORDER BY revision DESC LIMIT 1", (project_id,),
            )).fetchone()
            if current is None or current["status"] != "completed":
                await connection.rollback()
                raise ProjectConflictError("No completed search is available for paper selection")
            await connection.execute("UPDATE papers SET selected=0 WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM paper_selections WHERE project_id=?", (project_id,))
            await connection.execute("DELETE FROM analysis_reports WHERE project_id=?", (project_id,))
            await connection.execute(
                "DELETE FROM selected_paper_analyses WHERE project_id=?", (project_id,)
            )
            await connection.commit()

    async def save_paper_analysis(
        self, project_id: str, revision: int, analysis: PaperAnalysis
    ) -> None:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO selected_paper_analyses(project_id,search_revision,paper_id,payload_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,search_revision,"
                "paper_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (project_id, revision, analysis.paper_id, analysis.model_dump_json(), now, now),
            )
            await connection.commit()

    async def paper_analyses(self, project_id: str, revision: int) -> list[PaperAnalysis]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT payload_json FROM selected_paper_analyses WHERE project_id=? "
                "AND search_revision=? ORDER BY created_at", (project_id, revision),
            )).fetchall()
        return [PaperAnalysis.model_validate_json(row["payload_json"]) for row in rows]

    async def save_report(self, report: AnalysisReport) -> None:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO analysis_reports(project_id,search_revision,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(project_id,search_revision) DO UPDATE SET "
                "payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (report.project_id, report.search_revision, report.model_dump_json(), now, now),
            )
            await connection.commit()

    async def reset_analysis(self, project_id: str, revision: int) -> None:
        """Discard generated prose while retaining parsed documents and evidence."""
        async with self.database.connect() as connection:
            await connection.execute(
                "DELETE FROM analysis_reports WHERE project_id=? AND search_revision=?",
                (project_id, revision),
            )
            await connection.execute(
                "DELETE FROM selected_paper_analyses WHERE project_id=? AND search_revision=?",
                (project_id, revision),
            )
            await connection.execute(
                "DELETE FROM work_items WHERE project_id=? AND item_type='paper_analysis_section' "
                "AND run_scope LIKE ?",
                (project_id, f"paper-analysis:{revision}:%"),
            )
            await connection.commit()

    async def report(self, project_id: str, revision: int) -> AnalysisReport | None:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT payload_json FROM analysis_reports WHERE project_id=? AND search_revision=?",
                (project_id, revision),
            )).fetchone()
        return AnalysisReport.model_validate_json(row["payload_json"]) if row else None
