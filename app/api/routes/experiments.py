from typing import Annotated, Any

from fastapi import APIRouter, Depends
from langgraph.types import Command

from app.api.dependencies import (
    get_checkpointer,
    get_evidence_repository,
    get_llm_provider,
    get_project_repository,
    get_proposal_repository,
    get_skill_registry,
    get_trace_repository,
)
from app.db import EvidenceRepository, ProjectRepository, ProposalRepository, TraceRepository
from app.db.errors import ProjectConflictError
from app.experiments import ProposalBuilder, build_experiment_graph
from app.llm import LLMProvider
from app.schemas import (
    ExperimentProposal,
    ProposalCreateRequest,
    ProposalDecision,
    ProposalRunResponse,
)
from app.skills import SkillRegistry

router = APIRouter(prefix="/projects/{project_id}/experiment-proposal", tags=["experiments"])


@router.post("", response_model=ProposalRunResponse, status_code=201)
async def create_proposal(
    project_id: str,
    request: ProposalCreateRequest,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
    skills: Annotated[SkillRegistry, Depends(get_skill_registry)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    proposals: Annotated[ProposalRepository, Depends(get_proposal_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
) -> ProposalRunResponse:
    await projects.get(project_id)
    graph = build_experiment_graph(ProposalBuilder(provider, evidence, skills), checkpointer)
    config = {"configurable": {"thread_id": f"experiment:{project_id}"}}
    result = await graph.ainvoke(
        {
            "project_id": project_id,
            "objective": request.objective,
            "evidence_ids": request.evidence_ids,
            "proposal": None,
        },
        config=config,
    )
    proposal = ExperimentProposal.model_validate(result["proposal"])
    await proposals.create(proposal)
    await projects.wait(project_id, "human_approval")
    await traces.append(
        project_id,
        proposal.proposal_id,
        "skill_load",
        success=True,
        agent="research_builder",
        summary={"name": "experiment-design", "version": "1.0.0"},
    )
    await traces.append(
        project_id,
        proposal.proposal_id,
        "human_approval_interrupted",
        success=True,
        agent="research_builder",
        summary={"proposal_id": proposal.proposal_id, "version": proposal.version},
    )
    return ProposalRunResponse(proposal=proposal, interrupted=True)


@router.get("", response_model=ExperimentProposal)
async def get_proposal(
    project_id: str,
    proposals: Annotated[ProposalRepository, Depends(get_proposal_repository)],
) -> ExperimentProposal:
    return await proposals.get(project_id)


@router.post("/decision", response_model=ProposalRunResponse)
async def decide_proposal(
    project_id: str,
    decision: ProposalDecision,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
    skills: Annotated[SkillRegistry, Depends(get_skill_registry)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    proposals: Annotated[ProposalRepository, Depends(get_proposal_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
) -> ProposalRunResponse:
    current = await proposals.get(project_id)
    if current.version != decision.version:
        raise ProjectConflictError(
            f"Proposal version changed: expected {decision.version}, current {current.version}"
        )
    graph = build_experiment_graph(ProposalBuilder(provider, evidence, skills), checkpointer)
    config = {"configurable": {"thread_id": f"experiment:{project_id}"}}
    result = await graph.ainvoke(Command(resume=decision.model_dump(mode="json")), config=config)
    updated = ExperimentProposal.model_validate(result["proposal"])
    saved = await proposals.decide(project_id, decision, updated)
    await projects.complete(project_id, f"proposal_{saved.status}")
    await traces.append(
        project_id,
        saved.proposal_id,
        "human_approval_resumed",
        success=True,
        agent="research_builder",
        summary={"action": decision.action, "from_version": decision.version},
    )
    return ProposalRunResponse(proposal=saved, interrupted=False)
