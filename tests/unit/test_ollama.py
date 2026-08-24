import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.llm import LLMError, OllamaProvider


def make_provider(handler: httpx.MockTransport) -> OllamaProvider:
    settings = Settings(
        ollama_model="qwen3:14b",
        ollama_base_url="http://ollama.test",
        ollama_timeout_seconds=5,
    )
    client = httpx.AsyncClient(base_url=settings.ollama_base_url, transport=handler)
    return OllamaProvider(settings=settings, client=client)


@pytest.mark.asyncio
async def test_chat_returns_content() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={"message": {"content": "pong"}})

    provider = make_provider(httpx.MockTransport(respond))
    assert await provider.chat([{"role": "user", "content": "ping"}]) == "pong"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_structured_output_is_validated() -> None:
    class Answer(BaseModel):
        answer: str

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert payload["format"]["properties"]["answer"]["type"] == "string"
        assert "title" not in payload["format"]["properties"]["answer"]
        return httpx.Response(200, json={"message": {"content": '{"answer":"ok"}'}})

    provider = make_provider(httpx.MockTransport(respond))
    result = await provider.structured_output([{"role": "user", "content": "answer"}], Answer)
    assert result == Answer(answer="ok")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_http_error_becomes_provider_error() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found", request=request)

    provider = make_provider(httpx.MockTransport(respond))
    with pytest.raises(LLMError, match="HTTP 404"):
        await provider.chat([{"role": "user", "content": "hello"}])
    await provider._client.aclose()
