import logging
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.api.dependencies import get_llm_provider
from app.api.routes.models import ModelProbe
from app.core.logging import configure_logging
from app.llm import LLMError, LLMProvider
from app.main import app


class StubProvider(LLMProvider):
    def __init__(self, error: LLMError | None = None) -> None:
        self.error = error

    async def chat(self, messages: list[dict[str, str]]) -> str:
        return "ok"

    async def structured_output(self, messages, response_model):
        if self.error:
            raise self.error
        assert response_model is ModelProbe
        return ModelProbe(status="ok", response="model is available")

    async def close(self) -> None:
        return None


def override_provider(provider: LLMProvider):
    async def dependency() -> AsyncIterator[LLMProvider]:
        yield provider

    return dependency


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "research-pilot"}


def test_dependency_health_reports_openalex_configuration() -> None:
    with TestClient(app) as client:
        response = client.get("/health/dependencies")
    assert response.status_code == 200
    assert response.json()["ollama_configured"] is True
    assert isinstance(response.json()["openalex_api_key_configured"], bool)


def test_model_probe() -> None:
    app.dependency_overrides[get_llm_provider] = override_provider(StubProvider())
    try:
        with TestClient(app) as client:
            response = client.post("/models/test", json={"prompt": "Are you ready?"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "provider": "ollama",
        "model": "qwen3:14b",
        "response": "model is available",
    }


def test_model_probe_reports_unavailable_provider() -> None:
    app.dependency_overrides[get_llm_provider] = override_provider(
        StubProvider(LLMError("Ollama unavailable"))
    )
    try:
        with TestClient(app) as client:
            response = client.post("/models/test", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "MODEL_UNAVAILABLE"
    assert error["message"] == "Ollama unavailable"
    assert error["retryable"] is True
    assert response.headers["X-Request-ID"] == error["request_id"]


def test_request_id_is_preserved() -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "test-request-123"})
    assert response.headers["X-Request-ID"] == "test-request-123"


def test_validation_error_uses_common_shape() -> None:
    with TestClient(app) as client:
        response = client.post("/models/test", json={"prompt": ""})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["location"] == ["body", "prompt"]


def test_http_client_request_logs_are_suppressed_to_protect_query_credentials() -> None:
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpx2").level == logging.WARNING
