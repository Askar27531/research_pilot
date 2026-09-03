from typing import Protocol

from app.schemas import ExperimentProposal, MethodCard, ResearchProfile, TransferCandidateSet


class ArtifactWriter(Protocol):
    async def markdown(self, project_id: str, name: str, title: str,
                       sections: dict[str, str]): ...
    async def csv(self, project_id: str, name: str, columns: list[str],
                  rows: list[list[str]]): ...
    async def mermaid(self, project_id: str, name: str, diagram: str): ...


async def generate_transfer_artifacts(
    service: ArtifactWriter,
    profile: ResearchProfile,
    cards: list[MethodCard],
    candidates: TransferCandidateSet,
) -> None:
    await service.markdown(profile.project_id, "research-report", "Research Report", {
        "Research question": profile.problem_statement,
        "Current approach": profile.baseline or "Not specified",
        "Objectives": "\n".join(f"- {value}" for value in profile.objectives),
        "Evidence-backed directions": "\n\n".join(
            f"### {item.title}\n\n{item.target_challenge}\n\n"
            + "\n".join(f"- {step}" for step in item.integration_design)
            for item in candidates.combinations[:3]
        ) or "No sufficiently supported direction was identified.",
        "Limitations": "The proposed directions are hypotheses for validation, not novelty claims.",
    })
    await service.csv(profile.project_id, "literature-matrix",
        ["method", "target_problem", "mechanism_steps", "evidence_count"], [
            [card.method_name.value, card.target_problem.value,
             "; ".join(value.value for value in card.mechanism_steps),
             str(len({eid for value in card.mechanism_steps for eid in value.evidence_ids}))]
            for card in cards
        ])
    await service.csv(profile.project_id, "evidence-index",
        ["method", "supported_claim", "evidence_count"], [
            [card.method_name.value, value.value, str(len(value.evidence_ids))]
            for card in cards for value in card.mechanism_steps if value.kind == "supported_fact"
        ])
    await service.markdown(profile.project_id, "research-profile", "Research Profile", {
        "Problem": profile.problem_statement,
        "Objectives": "\n".join(f"- {x}" for x in profile.objectives),
        "Baseline": profile.baseline or "Not specified",
        "Constraints": "\n".join(f"- {x}" for x in profile.constraints) or "None specified",
        "Metrics": "\n".join(f"- {x}" for x in profile.metrics) or "None specified",
        "Pain points": "\n".join(f"- {x}" for x in profile.pain_points) or "None specified",
    })
    await service.csv(profile.project_id, "method-cards",
        ["paper_id", "method", "target_problem", "mechanism", "evidence_ids"], [
            [card.paper_id, card.method_name.value, card.target_problem.value,
             "; ".join(x.value for x in card.mechanism_steps),
             "; ".join(sorted({eid for x in card.mechanism_steps for eid in x.evidence_ids}))]
            for card in cards
        ])
    await service.csv(profile.project_id, "transfer-matrix",
        ["method_card_id", "decision", "target_challenge", "confidence", "evidence_ids"], [
            [item.method_card_id, item.decision, item.target_challenge, str(item.confidence),
             "; ".join(item.evidence_ids)] for item in candidates.assessments
        ])
    await service.markdown(profile.project_id, "combination-candidates",
        "Method Transfer and Combination Candidates", {
            item.title: (
                f"Hypothesis for: {item.target_challenge}\n\n" +
                "\n".join(f"- {step}" for step in item.integration_design) +
                f"\n\nNovelty risk: {item.novelty_risk}\n\nEvidence: {', '.join(item.evidence_ids)}"
            ) for item in candidates.combinations
        } or {"Result": "No compatible multi-method combination was supported."})


async def generate_experiment_artifacts(service: ArtifactWriter, proposal: ExperimentProposal) -> None:
    await service.markdown(proposal.project_id, "experiment-plan", "Experiment Plan", {
        experiment.title: (
            f"Baseline: {experiment.baseline}\n\nModification: {experiment.modification}\n\n"
            f"Metrics: {', '.join(experiment.metrics)}\n\nSuccess: {experiment.success_criterion}\n\n"
            f"Failure: {experiment.failure_criterion}\n\nEvidence: {', '.join(experiment.evidence_ids)}"
        ) for experiment in proposal.experiments
    })
    nodes = ['P["Accepted transfer hypothesis"]']
    for index, experiment in enumerate(proposal.experiments, 1):
        nodes.append(f'P --> E{index}["{experiment.title.replace(chr(34), chr(39))}"]')
        nodes.append(f'E{index} --> V{index}["Evaluate: {", ".join(experiment.metrics)}"]')
    await service.mermaid(proposal.project_id, "research-pipeline", "flowchart LR\n  " + "\n  ".join(nodes))
