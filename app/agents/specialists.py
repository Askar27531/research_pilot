from datetime import UTC, datetime
from uuid import uuid4

from app.db import DocumentRepository, EvidenceRepository, TransferRepository
from app.documents import DocumentService
from app.evidence import EvidenceBuilder
from app.llm import LLMProvider
from app.schemas import (
    AgentCapability,
    AgentResult,
    AgentTask,
    CombinationCandidate,
    MethodCardDraft,
    TextEvidenceCreate,
    TransferAssessment,
    TransferCandidateSet,
    TransferCandidateSetDraft,
)
from app.skills import SkillRegistry


class MultimodalAnalyst:
    name = "multimodal_analyst"

    def __init__(self, provider: LLMProvider | None = None,
                 documents: DocumentService | None = None,
                 document_repository: DocumentRepository | None = None,
                 evidence: EvidenceRepository | None = None,
                 transfer: TransferRepository | None = None,
                 skills: SkillRegistry | None = None) -> None:
        self.provider, self.documents = provider, documents
        self.document_repository, self.evidence = document_repository, evidence
        self.transfer, self.skills = transfer, skills

    def capability(self) -> AgentCapability:
        return AgentCapability(agent=self.name, supported_tasks=["multimodal_analysis"],
            available=True, description="Extract verified evidence and method mechanism cards")

    async def run(self, task: AgentTask) -> AgentResult:
        if not all((self.provider, self.documents, self.document_repository,
                    self.evidence, self.transfer, self.skills)):
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="failed", summary="Analyst dependencies are unavailable",
                error={"code": "ANALYST_NOT_CONFIGURED"})
        cards = []
        builder = EvidenceBuilder(self.documents, self.document_repository, self.evidence)
        skill = self.skills.load_skill("method-mechanism-extraction")
        try:
            for item in task.context.get("documents", []):
                paper_id, document_id = item["paper_id"], item["document_id"]
                parsed = self.documents.get_structure(task.project_id, document_id)
                nodes = []
                for page in [page for page in parsed.pages if page.text.strip()][:8]:
                    quote = page.text.strip()[:900]
                    nodes.append(await builder.build_text(task.project_id, TextEvidenceCreate(
                        paper_id=paper_id, document_id=document_id, page_number=page.page_number,
                        claim="Verified source passage for method mechanism extraction",
                        quote=quote, confidence=1.0)))
                if not nodes:
                    continue
                index = "\n".join(f"{n.evidence_id} [page {n.page_number}]: {n.excerpt}" for n in nodes)
                draft = await self.provider.structured_output([
                    {"role": "system", "content": skill.content},
                    {"role": "user", "content": (
                        "Create a domain-neutral method card. Facts may cite only the verified "
                        "evidence IDs below. Unestablished fields must be inference without evidence. "
                        f"paper_id={paper_id}\n{index}")},
                ], MethodCardDraft)
                draft = draft.model_copy(update={"paper_id": paper_id})
                allowed = {node.evidence_id for node in nodes}
                cited = {eid for value in self._fields(draft) for eid in value.evidence_ids}
                if not cited <= allowed:
                    raise ValueError("Method card cites evidence outside the verified index")
                cards.append(await self.transfer.upsert_method_card(task.project_id, draft))
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="completed", summary=f"Created {len(cards)} method cards",
                output={"method_cards": [card.model_dump(mode="json") for card in cards]},
                loaded_skills=[skill.name])
        except Exception as exc:  # noqa: BLE001 - agent boundary returns structured failure
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="failed", summary="Method analysis failed",
                error={"type": type(exc).__name__, "message": str(exc)[:1000]})

    @staticmethod
    def _fields(card: MethodCardDraft):
        return [card.method_name, card.target_problem, *card.mechanism_steps, *card.inputs,
            *card.outputs, *card.key_components, *card.assumptions,
            *card.resource_requirements, *card.evaluation_context, *card.strengths,
            *card.limitations]


class ResearchBuilder:
    name = "research_builder"

    def __init__(self, provider: LLMProvider | None = None,
                 transfer: TransferRepository | None = None,
                 evidence: EvidenceRepository | None = None,
                 skills: SkillRegistry | None = None) -> None:
        self.provider, self.transfer, self.evidence, self.skills = provider, transfer, evidence, skills

    def capability(self) -> AgentCapability:
        return AgentCapability(agent=self.name, supported_tasks=["research_build"],
            available=True, description="Assess method transfer and construct falsifiable combinations")

    async def run(self, task: AgentTask) -> AgentResult:
        if not all((self.provider, self.transfer, self.evidence, self.skills)):
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="failed", summary="Builder dependencies are unavailable",
                error={"code": "BUILDER_NOT_CONFIGURED"})
        try:
            profile = await self.transfer.get_profile(task.project_id)
            cards = await self.transfer.list_method_cards(task.project_id)
            if not cards:
                raise ValueError("At least one method card is required")
            transfer_skill = self.skills.load_skill("method-transfer-analysis")
            combination_skill = self.skills.load_skill("method-combination-design")
            excluded_ids = await self.evidence.excluded_ids(task.project_id)
            cards_payload = []
            for card in cards:
                for field in MultimodalAnalyst._fields(card):
                    field.evidence_ids[:] = [
                        value for value in field.evidence_ids if value not in excluded_ids
                    ]
                    if field.kind == "supported_fact" and not field.evidence_ids:
                        field.kind = "inference"
                cards_payload.append(card.model_dump_json())
            draft = await self.provider.structured_output([
                {"role": "system", "content": transfer_skill.content + "\n\n" + combination_skill.content},
                {"role": "user", "content": (
                    "Assess every method as adapt, combine, or reject. Suggestions are hypotheses, "
                    "not paper facts. Never claim novelty. Include risks and falsification tests.\n"
                    f"PROFILE:\n{profile.model_dump_json()}\nMETHOD CARDS:\n" +
                    "\n".join(cards_payload) +
                    (f"\nUSER REVISION REQUEST:\n{task.context['feedback']}"
                     if task.context.get("feedback") else ""))},
            ], TransferCandidateSetDraft)
            card_ids = {card.card_id for card in cards}
            evidence_ids = {eid for card in cards for value in MultimodalAnalyst._fields(card)
                            for eid in value.evidence_ids if eid not in excluded_ids}
            for item in draft.assessments:
                if item.method_card_id not in card_ids or not set(item.evidence_ids) <= evidence_ids:
                    raise ValueError("Transfer assessment has an invalid reference")
            for item in draft.combinations:
                if not set(item.method_card_ids) <= card_ids or not set(item.evidence_ids) <= evidence_ids:
                    raise ValueError("Combination has an invalid reference")
            now = datetime.now(UTC).isoformat()
            try:
                current = await self.transfer.get_candidates(task.project_id)
            except Exception:  # noqa: BLE001 - missing candidates are an expected first-run state
                current = None
            result = TransferCandidateSet(
                candidate_set_id=current.candidate_set_id if current else str(uuid4()),
                project_id=task.project_id,
                revision=current.revision + 1 if current else 1, status="pending",
                assessments=[TransferAssessment(**x.model_dump(), assessment_id=str(uuid4()))
                             for x in draft.assessments],
                combinations=[CombinationCandidate(**x.model_dump(), candidate_id=str(uuid4()))
                              for x in draft.combinations], created_at=now, updated_at=now)
            if task.context.get("persist", True):
                await self.transfer.save_candidates(result)
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="completed", summary="Transfer candidates created",
                output={"transfer_candidates": result.model_dump(mode="json")},
                loaded_skills=[transfer_skill.name, combination_skill.name])
        except Exception as exc:  # noqa: BLE001 - agent boundary returns structured failure
            return AgentResult(task_id=task.task_id, project_id=task.project_id,
                agent=self.name, status="failed", summary="Transfer analysis failed",
                error={"type": type(exc).__name__, "message": str(exc)[:1000]})
