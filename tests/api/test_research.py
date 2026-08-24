from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.api.dependencies import get_llm_provider
from app.llm import LLMError, LLMProvider
from app.main import app
from app.schemas import ResearchUnderstanding


class ResearchProvider(LLMProvider):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def chat(self, messages) -> str:
        return "unused"

    async def structured_output(self, messages, response_model):
        if self.fail:
            raise LLMError("Ollama timed out")
        return ResearchUnderstanding(
            normalized_goal="Study RGB-LWIR image registration",
            core_concepts=["RGB-LWIR", "image registration"],
            domain="computer vision",
            year_from=2024,
            year_to=2026,
            ambiguities=[],
        )

    async def close(self) -> None:
        return None


def provider_override(provider: LLMProvider):
    async def dependency() -> AsyncIterator[LLMProvider]:
        yield provider

    return dependency


def test_research_graph_endpoint() -> None:
    app.dependency_overrides[get_llm_provider] = provider_override(ResearchProvider())
    try:
        with TestClient(app) as client:
            response = client.post(
                "/research/test",
                json={
                    "research_question": "RGB-LWIR image registration",
                    "year_from": 2024,
                    "year_to": 2026,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["current_stage"] == "request_understood"
    assert body["understanding"]["core_concepts"] == ["RGB-LWIR", "image registration"]


def test_research_graph_endpoint_maps_model_failure() -> None:
    app.dependency_overrides[get_llm_provider] = provider_override(ResearchProvider(fail=True))
    try:
        with TestClient(app) as client:
            response = client.post(
                "/research/test",
                json={"research_question": "RGB-LWIR registration"},
                headers={"X-Request-ID": "research-failure"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "MODEL_UNAVAILABLE",
        "message": "Ollama timed out",
        "retryable": True,
        "request_id": "research-failure",
    }
