from uuid import uuid4

from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import (
    MethodCard,
    MethodCardDraft,
    PaperAcquisition,
    ResearchProfile,
    ResearchProfileInput,
    TransferCandidateSet,
    TransferDecision,
)


class TransferRepository:
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

    async def upsert_method_card(
        self, project_id: str, draft: MethodCardDraft
    ) -> MethodCard:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (await connection.execute(
                "SELECT id,revision,created_at FROM method_cards WHERE project_id=? AND paper_id=?",
                (project_id, draft.paper_id),
            )).fetchone()
            identifier = row["id"] if row else str(uuid4())
            revision = row["revision"] + 1 if row else 1
            created = row["created_at"] if row else now
            card = MethodCard(**draft.model_dump(), card_id=identifier, project_id=project_id,
                              revision=revision, created_at=created, updated_at=now)
            await connection.execute(
                """INSERT INTO method_cards(id,project_id,paper_id,revision,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,paper_id) DO UPDATE SET
                revision=excluded.revision,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (identifier, project_id, draft.paper_id, revision, card.model_dump_json(), created, now),
            )
            await connection.commit()
        return card

    async def list_method_cards(self, project_id: str) -> list[MethodCard]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT payload_json FROM method_cards WHERE project_id=? ORDER BY updated_at,id",
                (project_id,),
            )).fetchall()
        return [MethodCard.model_validate_json(row["payload_json"]) for row in rows]

    async def save_candidates(self, candidates: TransferCandidateSet) -> TransferCandidateSet:
        async with self.database.connect() as connection:
            await connection.execute(
                """INSERT INTO transfer_candidate_sets(id,project_id,revision,status,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET id=excluded.id,
                revision=excluded.revision,status=excluded.status,payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
                (candidates.candidate_set_id, candidates.project_id, candidates.revision,
                 candidates.status, candidates.model_dump_json(), candidates.created_at,
                 candidates.updated_at),
            )
            await connection.commit()
        return candidates

    async def get_candidates(self, project_id: str) -> TransferCandidateSet:
        return await self._get_payload(
            "SELECT payload_json FROM transfer_candidate_sets WHERE project_id=?", (project_id,),
            TransferCandidateSet, "Transfer candidates not found"
        )

    async def decide_candidates(
        self, project_id: str, decision: TransferDecision
    ) -> TransferCandidateSet:
        current = await self.get_candidates(project_id)
        if current.revision != decision.revision:
            raise ProjectConflictError(
                f"Transfer revision changed: expected {decision.revision}, current {current.revision}"
            )
        if current.status != "pending":
            raise ProjectConflictError("Transfer candidates have already been decided")
        combinations = current.combinations
        if decision.selected_candidate_id:
            selected = [item for item in combinations
                        if item.candidate_id == decision.selected_candidate_id]
            if not selected:
                raise ProjectConflictError("Selected transfer candidate does not exist")
            combinations = selected + [item for item in combinations
                                       if item.candidate_id != decision.selected_candidate_id]
        status = "modified" if decision.action == "modify" else f"{decision.action}ed"
        updated = current.model_copy(update={
            "revision": current.revision + 1, "status": status,
            "combinations": combinations,
            "decision_feedback": decision.feedback, "updated_at": utc_now(),
        })
        return await self.save_candidates(updated)

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
