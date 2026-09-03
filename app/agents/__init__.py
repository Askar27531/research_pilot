from app.agents.coordinator import Coordinator
from app.agents.literature import LiteratureResearcher
from app.agents.paper_analysis import EvidenceSynthesizer, PaperAnalyst

# Product-facing name; legacy import remains valid for internal checkpoints and scripts.
ResearchCoordinator = Coordinator

__all__ = [
    "Coordinator",
    "EvidenceSynthesizer",
    "LiteratureResearcher",
    "PaperAnalyst",
    "ResearchCoordinator",
]
