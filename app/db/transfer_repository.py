from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import PaperAcquisition


class TransferRepository:
    """Persistence for per-paper PDF acquisitions."""

    def __init__(self, database: Database) -> None:
        self.database = database

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
