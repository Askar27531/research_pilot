from app.agents.coordinator import Coordinator
from app.agents.literature import LiteratureResearcher
from app.agents.paper_analysis import EvidenceSynthesizer, PaperAnalyst
from app.agents.specialists import MultimodalAnalyst, ResearchBuilder

# Product-facing names; legacy imports remain valid for internal checkpoints and scripts.
ResearchCoordinator = Coordinator
MultimodalPaperAnalyst = MultimodalAnalyst
ResearchPlanner = ResearchBuilder

__all__ = [
    "Coordinator",
    "EvidenceSynthesizer",
    "LiteratureResearcher",
    "MultimodalAnalyst",
    "MultimodalPaperAnalyst",
    "PaperAnalyst",
    "ResearchBuilder",
    "ResearchCoordinator",
    "ResearchPlanner",
]
