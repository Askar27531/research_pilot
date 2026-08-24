import sqlite3

from app.db.database import Database
from app.db.errors import EvidenceReferencedError, RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import DocumentEntry, EvidenceNode, LinkedDocument, PaperSummary


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

    async def delete(self, project_id: str, evidence_id: str) -> None:
        try:
            async with self.database.connect() as connection:
                cursor = await connection.execute(
                    "DELETE FROM evidence WHERE id=? AND project_id=?",
                    (evidence_id, project_id),
                )
                if cursor.rowcount == 0:
                    raise RecordNotFoundError(f"Evidence not found: {evidence_id}")
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise EvidenceReferencedError("Evidence is referenced by a paper summary") from exc


class SummaryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert(self, summary: PaperSummary) -> PaperSummary:
        evidence_ids = sorted(
            {
                evidence_id
                for field in (
                    summary.method,
                    summary.datasets,
                    summary.metrics,
                    summary.contributions,
                    summary.limitations,
                )
                for claim in field
                for evidence_id in claim.evidence_ids
            }
        )
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            for evidence_id in evidence_ids:
                row = await (
                    await connection.execute(
                        "SELECT 1 FROM evidence WHERE id=? AND project_id=? AND paper_id=?",
                        (evidence_id, summary.project_id, summary.paper_id),
                    )
                ).fetchone()
                if row is None:
                    await connection.rollback()
                    raise RecordNotFoundError(
                        f"Evidence does not belong to summary paper: {evidence_id}"
                    )
            existing = await (
                await connection.execute(
                    "SELECT id FROM paper_summaries WHERE project_id=? AND paper_id=?",
                    (summary.project_id, summary.paper_id),
                )
            ).fetchone()
            summary_id = existing["id"] if existing else summary.summary_id
            payload = summary.model_copy(update={"summary_id": summary_id})
            await connection.execute(
                """
                INSERT INTO paper_summaries(id, project_id, paper_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, paper_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (
                    summary_id,
                    summary.project_id,
                    summary.paper_id,
                    payload.model_dump_json(),
                    summary.created_at,
                ),
            )
            await connection.execute(
                "DELETE FROM summary_evidence_refs WHERE summary_id=?", (summary_id,)
            )
            await connection.executemany(
                "INSERT INTO summary_evidence_refs(summary_id, evidence_id) VALUES (?, ?)",
                [(summary_id, evidence_id) for evidence_id in evidence_ids],
            )
            await connection.commit()
        return payload

    async def get(self, project_id: str, paper_id: str) -> PaperSummary:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT payload_json FROM paper_summaries WHERE project_id=? AND paper_id=?",
                    (project_id, paper_id),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Paper summary not found: {paper_id}")
        return PaperSummary.model_validate_json(row["payload_json"])

    async def list_for_project(self, project_id: str) -> list[PaperSummary]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT payload_json FROM paper_summaries WHERE project_id=? ORDER BY paper_id",
                    (project_id,),
                )
            ).fetchall()
        return [PaperSummary.model_validate_json(row["payload_json"]) for row in rows]
