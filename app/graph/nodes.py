import re
from collections.abc import Awaitable, Callable

from app.graph.state import ResearchState
from app.llm import LLMProvider
from app.schemas import ResearchRequest, ResearchUnderstanding

GraphNode = Callable[[ResearchState], Awaitable[dict[str, object]]]


def make_understand_request_node(provider: LLMProvider) -> GraphNode:
    """Build a request-understanding node with an injected model provider."""

    async def understand_request(state: ResearchState) -> dict[str, object]:
        request = ResearchRequest.model_validate(state["request"])
        understanding = await provider.structured_output(
            [
                {
                    "role": "system",
                    "content": (
                        "Interpret a scientific research request. Preserve explicit constraints, "
                        "especially the year range. Use the same language as the user for all "
                        "natural-language fields. Return ambiguities=[] when the request is clear. "
                        "Return concise structured data only."
                    ),
                },
                {"role": "user", "content": request.model_dump_json()},
            ],
            ResearchUnderstanding,
        )

        # Explicit user constraints always take precedence over model inference.
        if request.year_from is not None:
            understanding.year_from = request.year_from
        if request.year_to is not None:
            understanding.year_to = request.year_to
        understanding.ambiguities = _sanitize_ambiguities(
            request.research_question, understanding.ambiguities
        )

        return {
            "understanding": understanding.model_dump(mode="json"),
            "current_stage": "request_understood",
        }

    return understand_request


def _sanitize_ambiguities(user_goal: str, ambiguities: list[str]) -> list[str]:
    if not _contains_cjk(user_goal):
        return ambiguities
    return [
        item for item in ambiguities if _contains_cjk(item) and "??" not in item and "�" not in item
    ]


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value))
