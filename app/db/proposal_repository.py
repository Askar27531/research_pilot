from uuid import uuid4

from app.db.database import Database
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.db.repositories import utc_now
from app.schemas import ExperimentProposal, ProposalDecision


class ProposalRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, proposal: ExperimentProposal) -> ExperimentProposal:
        async with self.database.connect() as connection:
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM experiment_proposals WHERE project_id=?",
                    (proposal.project_id,),
                )
            ).fetchone()
            if existing:
                raise ProjectConflictError("Project already has an experiment proposal")
            await connection.execute(
                """
                INSERT INTO experiment_proposals(
                    id, project_id, version, status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.project_id,
                    proposal.version,
                    proposal.status,
                    proposal.model_dump_json(),
                    proposal.created_at,
                    proposal.updated_at,
                ),
            )
            await connection.commit()
        return proposal

    async def get(self, project_id: str) -> ExperimentProposal:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT payload_json FROM experiment_proposals WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("Experiment proposal not found")
        return ExperimentProposal.model_validate_json(row["payload_json"])

    async def decide(
        self,
        project_id: str,
        decision: ProposalDecision,
        updated: ExperimentProposal,
    ) -> ExperimentProposal:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    "SELECT id, version, status FROM experiment_proposals WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
            if row is None:
                await connection.rollback()
                raise RecordNotFoundError("Experiment proposal not found")
            if row["version"] != decision.version:
                await connection.rollback()
                raise ProjectConflictError(
                    f"Proposal version changed: expected {decision.version}, current {row['version']}"
                )
            if row["status"] != "pending":
                await connection.rollback()
                raise ProjectConflictError("Proposal has already been decided")
            await connection.execute(
                """
                UPDATE experiment_proposals
                SET version=?, status=?, payload_json=?, updated_at=?
                WHERE project_id=? AND version=?
                """,
                (
                    updated.version,
                    updated.status,
                    updated.model_dump_json(),
                    now,
                    project_id,
                    decision.version,
                ),
            )
            await connection.execute(
                """
                INSERT INTO proposal_decisions(
                    id, proposal_id, from_version, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    row["id"],
                    decision.version,
                    decision.action,
                    decision.model_dump_json(),
                    now,
                ),
            )
            await connection.commit()
        return updated
