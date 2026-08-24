from datetime import UTC, datetime
from uuid import uuid4

from app.db import EvidenceRepository
from app.llm import LLMProvider
from app.schemas import ExperimentProposal, ExperimentProposalDraft
from app.skills import SkillRegistry


class ProposalBuilder:
    def __init__(
        self, provider: LLMProvider, evidence: EvidenceRepository, skills: SkillRegistry
    ) -> None:
        self.provider = provider
        self.evidence = evidence
        self.skills = skills

    async def build(
        self, project_id: str, objective: str, evidence_ids: list[str]
    ) -> ExperimentProposal:
        nodes = [await self.evidence.get(project_id, evidence_id) for evidence_id in evidence_ids]
        skill = self.skills.load_skill("experiment-design")
        draft = await self.provider.structured_output(
            [
                {"role": "system", "content": f"Apply this workflow skill:\n\n{skill.content}"},
                {
                    "role": "user",
                    "content": (
                        f"Objective: {objective}\nVerified evidence index:\n"
                        + "\n".join(
                            f"{node.evidence_id}: {node.claim} [page {node.page_number}]"
                            for node in nodes
                        )
                    ),
                },
            ],
            ExperimentProposalDraft,
        )
        allowed = set(evidence_ids)
        cited = {
            evidence_id
            for item in [*draft.hypotheses, *draft.experiments]
            for evidence_id in item.evidence_ids
        }
        if not cited <= allowed:
            raise ValueError("Proposal cites evidence outside the verified input set")
        now = datetime.now(UTC).isoformat()
        return ExperimentProposal(
            **draft.model_dump(),
            proposal_id=str(uuid4()),
            project_id=project_id,
            version=1,
            status="pending",
            created_at=now,
            updated_at=now,
        )
