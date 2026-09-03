from uuid import uuid4

from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import PaperAcquisition, ResearchProfile, ResearchProfileInput


class TransferRepository:
    """Persistence for project research profile and per-paper PDF acquisitions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_profile(
        self, project_id: str, value: ResearchProfileInput
    ) -> ResearchProfile:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            project = await (await connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            )).fetchone()
            if project is None:
                raise RecordNotFoundError(f"Project not found: {project_id}")
            row = await (await connection.execute(
                "SELECT id, revision, created_at FROM research_profiles WHERE project_id=?",
                (project_id,),
            )).fetchone()
            if row and value.expected_revision is not None and row["revision"] != value.expected_revision:
                raise ProjectConflictError(
                    f"Profile revision changed: expected {value.expected_revision}, current {row['revision']}"
                )
            identifier = row["id"] if row else str(uuid4())
            revision = row["revision"] + 1 if row else 1
            created = row["created_at"] if row else now
            profile = ResearchProfile(
                **value.model_dump(exclude={"expected_revision"}),
                expected_revision=None,
                profile_id=identifier,
                project_id=project_id,
                revision=revision,
                created_at=created,
                updated_at=now,
            )
            await connection.execute(
                """INSERT INTO research_profiles(id,project_id,revision,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                revision=excluded.revision,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (identifier, project_id, revision, profile.model_dump_json(), created, now),
            )
            await connection.commit()
        return profile

    async def get_profile(self, project_id: str) -> ResearchProfile:
        return await self._get_payload(
            "SELECT payload_json FROM research_profiles WHERE project_id=?",
            (project_id,), ResearchProfile, "Research profile not found"
        )

    async def upsert_acquisition(self, acquisition: PaperAcquisition) -> PaperAcquisition:
        async with self.database.connect() as connection:
            await connection.execute(
                """INSERT INTO paper_acquisitions(project_id,paper_id,status,payload_json,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(project_id,paper_id) DO UPDATE SET
                status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (acquisition.project_id, acquisition.paper_id, acquisition.status,
                 acquisition.model_dump_json(), acquisition.updated_at),
            )
            await connection.commit()
        return acquisition

    async def list_acquisitions(self, project_id: str) -> list[PaperAcquisition]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT payload_json FROM paper_acquisitions WHERE project_id=? ORDER BY paper_id",
                (project_id,),
            )).fetchall()
        return [PaperAcquisition.model_validate_json(row["payload_json"]) for row in rows]

    async def _get_payload(self, sql, params, model, missing):
        async with self.database.connect() as connection:
            row = await (await connection.execute(sql, params)).fetchone()
        if row is None:
            raise RecordNotFoundError(missing)
        return model.model_validate_json(row["payload_json"])
