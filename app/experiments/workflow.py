from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.experiments.builder import ProposalBuilder
from app.schemas import ExperimentProposal, ProposalDecision


class ExperimentState(TypedDict):
    project_id: str
    objective: str
    evidence_ids: list[str]
    proposal: dict[str, Any] | None


def apply_decision(proposal: ExperimentProposal, decision: ProposalDecision) -> ExperimentProposal:
    if proposal.version != decision.version:
        raise ValueError(
            f"Decision version {decision.version} does not match proposal {proposal.version}"
        )
    experiments = proposal.experiments
    if decision.action == "modify":
        updates = {item.experiment_id: item for item in decision.experiment_updates}
        unknown = set(updates) - {item.experiment_id for item in experiments}
        if unknown:
            raise ValueError(f"Unknown experiment IDs: {sorted(unknown)}")
        experiments = [
            item.model_copy(
                update={
                    key: value
                    for key, value in updates[item.experiment_id]
                    .model_dump(exclude_none=True)
                    .items()
                    if key != "experiment_id"
                }
            )
            if item.experiment_id in updates
            else item
            for item in experiments
        ]
    status = {"accept": "accepted", "modify": "modified", "reject": "rejected"}[decision.action]
    payload = proposal.model_dump(mode="json")
    payload.update(
        {
            "experiments": [item.model_dump(mode="json") for item in experiments],
            "status": status,
            "version": proposal.version + 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "decision_feedback": decision.feedback,
        }
    )
    return ExperimentProposal.model_validate(payload)


def build_experiment_graph(builder: ProposalBuilder, checkpointer: Any) -> Any:
    async def build_proposal(state: ExperimentState) -> dict[str, Any]:
        proposal = await builder.build(
            state["project_id"], state["objective"], state["evidence_ids"]
        )
        return {"proposal": proposal.model_dump(mode="json")}

    def human_approval(state: ExperimentState) -> dict[str, Any]:
        proposal = ExperimentProposal.model_validate(state["proposal"])
        raw = interrupt(
            {
                "proposal_id": proposal.proposal_id,
                "version": proposal.version,
                "status": proposal.status,
            }
        )
        decision = ProposalDecision.model_validate(raw)
        return {"proposal": apply_decision(proposal, decision).model_dump(mode="json")}

    graph = StateGraph(ExperimentState)
    graph.add_node("build_proposal", build_proposal)
    graph.add_node("human_approval", human_approval)
    graph.add_edge(START, "build_proposal")
    graph.add_edge("build_proposal", "human_approval")
    graph.add_edge("human_approval", END)
    return graph.compile(checkpointer=checkpointer)
