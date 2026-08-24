from app.schemas import AgentCapability, AgentResult, AgentTask


class UnsupportedSpecialist:
    name: str
    supported_task: str
    description: str

    def capability(self) -> AgentCapability:
        return AgentCapability(
            agent=self.name,
            supported_tasks=[self.supported_task],
            available=False,
            description=self.description,
        )

    async def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            task_id=task.task_id,
            project_id=task.project_id,
            agent=self.name,
            status="unsupported",
            summary=f"{self.name} is reserved for a later development stage",
        )


class MultimodalAnalyst(UnsupportedSpecialist):
    name = "multimodal_analyst"
    supported_task = "multimodal_analysis"
    description = "Reserved for P4 document and figure analysis"


class ResearchBuilder(UnsupportedSpecialist):
    name = "research_builder"
    supported_task = "research_build"
    description = "Reserved for P5/P6 evidence synthesis and experiment design"
