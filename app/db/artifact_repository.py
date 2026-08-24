from uuid import uuid4

from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import ArtifactRecord, ArtifactType


class ArtifactRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        project_id: str,
        artifact_type: ArtifactType,
        name: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        identifier = artifact_id or str(uuid4())
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            project = await (
                await connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,))
            ).fetchone()
            if project is None:
                await connection.rollback()
                raise RecordNotFoundError(f"Project not found: {project_id}")
            row = await (
                await connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE project_id=? AND name=?",
                    (project_id, name),
                )
            ).fetchone()
            version = row["version"]
            await connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, artifact_type, name, relative_path,
                    sha256, version, size_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    artifact_type,
                    name,
                    relative_path,
                    sha256,
                    version,
                    size_bytes,
                    now,
                ),
            )
            await connection.commit()
        return ArtifactRecord(
            artifact_id=identifier,
            project_id=project_id,
            artifact_type=artifact_type,
            name=name,
            relative_path=relative_path,
            sha256=sha256,
            version=version,
            size_bytes=size_bytes,
            created_at=now,
        )

    async def get(self, project_id: str, artifact_id: str) -> ArtifactRecord:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM artifacts WHERE id=? AND project_id=?",
                    (artifact_id, project_id),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Artifact not found: {artifact_id}")
        return self._record(row)

    async def list_for_project(self, project_id: str) -> list[ArtifactRecord]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at DESC, id",
                    (project_id,),
                )
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["id"],
            project_id=row["project_id"],
            artifact_type=row["artifact_type"],
            name=row["name"],
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            version=row["version"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
        )
