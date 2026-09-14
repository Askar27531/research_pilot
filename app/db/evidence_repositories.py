from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import DocumentEntry, EvidenceNode, LinkedDocument


class DocumentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def register(
        self, project_id: str, paper_id: str, entry: DocumentEntry
    ) -> LinkedDocument:
        now = utc_now()
        async with self.database.connect() as connection:
            paper = await (
                await connection.execute(
                    "SELECT 1 FROM papers WHERE id=? AND project_id=?", (paper_id, project_id)
                )
            ).fetchone()
            if paper is None:
                raise RecordNotFoundError(f"Paper not found in project: {paper_id}")
            await connection.execute(
                """
                INSERT INTO documents(id, project_id, paper_id, sha256, relative_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, paper_id) DO UPDATE SET
                    id=excluded.id, sha256=excluded.sha256,
                    relative_path=excluded.relative_path, created_at=excluded.created_at
                """,
                (
                    entry.document_id,
                    project_id,
                    paper_id,
                    entry.sha256,
                    entry.relative_path,
                    now,
                ),
            )
            await connection.commit()
        return await self.get(project_id, entry.document_id)

    async def get(self, project_id: str, document_id: str) -> LinkedDocument:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM documents WHERE id=? AND project_id=?",
                    (document_id, project_id),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Document not linked in project: {document_id}")
        return LinkedDocument(
            document_id=row["id"],
            project_id=row["project_id"],
            paper_id=row["paper_id"],
            sha256=row["sha256"],
            relative_path=row["relative_path"],
            created_at=row["created_at"],
        )

    async def get_for_paper(self, project_id: str, paper_id: str) -> LinkedDocument:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT id FROM documents WHERE project_id=? AND paper_id=?",
                (project_id, paper_id),
            )).fetchone()
        if row is None:
            raise RecordNotFoundError("Document not linked for selected paper")
        return await self.get(project_id, row["id"])


class EvidenceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, node: EvidenceNode, locator_key: str) -> EvidenceNode:
        async with self.database.connect() as connection:
            document = await (
                await connection.execute(
                    """
                    SELECT 1 FROM documents
                    WHERE id=? AND project_id=? AND paper_id=?
                    """,
                    (node.document_id, node.project_id, node.paper_id),
                )
            ).fetchone()
            if document is None:
                raise RecordNotFoundError("Paper and document are not linked in this project")
            existing = await (
                await connection.execute(
                    "SELECT id FROM evidence WHERE project_id=? AND paper_id=? AND locator_key=?",
                    (node.project_id, node.paper_id, locator_key),
                )
            ).fetchone()
            if existing is not None:
                node = node.model_copy(update={"evidence_id": existing["id"]})
            await connection.execute(
                """
                INSERT INTO evidence(
                    id, project_id, paper_id, document_id, evidence_type,
                    locator_key, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, paper_id, locator_key) DO UPDATE SET
                    payload_json=excluded.payload_json
                """,
                (
                    node.evidence_id,
                    node.project_id,
                    node.paper_id,
                    node.document_id,
                    node.evidence_type,
                    locator_key,
                    node.model_dump_json(),
                    node.created_at,
                ),
            )
            await connection.commit()
            row = await (
                await connection.execute(
                    "SELECT payload_json FROM evidence WHERE project_id=? AND paper_id=? AND locator_key=?",
                    (node.project_id, node.paper_id, locator_key),
                )
            ).fetchone()
        return EvidenceNode.model_validate_json(row["payload_json"])

    async def get(self, project_id: str, evidence_id: str) -> EvidenceNode:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT payload_json FROM evidence WHERE id=? AND project_id=?",
                    (evidence_id, project_id),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Evidence not found: {evidence_id}")
        return EvidenceNode.model_validate_json(row["payload_json"])

    async def list_for_paper(
        self, project_id: str, paper_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[EvidenceNode]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT payload_json FROM evidence
                    WHERE project_id=? AND paper_id=?
                    ORDER BY created_at, id LIMIT ? OFFSET ?
                    """,
                    (project_id, paper_id, limit, offset),
                )
            ).fetchall()
        return [EvidenceNode.model_validate_json(row["payload_json"]) for row in rows]

    async def excluded_ids(self, project_id: str) -> set[str]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT evidence_id FROM evidence_reviews "
                "WHERE project_id=? AND status='excluded'",
                (project_id,),
            )).fetchall()
        return {row["evidence_id"] for row in rows}

    async def delete(self, project_id: str, evidence_id: str) -> None:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM evidence WHERE id=? AND project_id=?",
                (evidence_id, project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Evidence not found: {evidence_id}")
            await connection.commit()
