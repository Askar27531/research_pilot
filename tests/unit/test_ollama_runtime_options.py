import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.llm.ollama import OllamaProvider


class RecordingClient:
    def __init__(self) -> None:
        self.calls = []

    async def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json={"message": {"content": "ok"}},
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )


class TinyResult(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_ollama_requests_apply_bounded_context_output_and_model_timeouts() -> None:
    client = RecordingClient()
    settings = Settings(
        ollama_model="qwen3-vl:8b",
        ollama_vision_model="qwen3-vl:8b",
        ollama_timeout_seconds=240,
        ollama_vision_timeout_seconds=180,
        ollama_num_ctx=8192,
        ollama_num_predict=1536,
        ollama_vision_num_ctx=4096,
        ollama_vision_num_predict=512,
    )
    provider = OllamaProvider(settings, client)  # type: ignore[arg-type]

    await provider.chat([{"role": "user", "content": "analyze"}])
    await provider._request(
        [{"role": "user", "content": "inspect"}], model="qwen3-vl:8b"
    )

    text_call = client.calls[0][1]
    assert text_call["json"]["options"] == {"num_ctx": 8192, "num_predict": 1536}
    assert text_call["timeout"] == 240
    vision_call = client.calls[1][1]
    assert vision_call["json"]["options"] == {"num_ctx": 4096, "num_predict": 512}
    assert vision_call["timeout"] == 180


@pytest.mark.asyncio
async def test_bounded_structured_output_uses_request_specific_output_budget() -> None:
    client = RecordingClient()
    client.post = _structured_post(client)  # type: ignore[method-assign]
    provider = OllamaProvider(Settings(
        ollama_model="qwen3:latest",
        ollama_vision_model="qwen3-vl:8b",
        ollama_num_predict=1536,
    ), client)  # type: ignore[arg-type]

    result = await provider.structured_output_bounded(
        [{"role": "user", "content": "analyze"}], TinyResult, max_output_tokens=768
    )

    assert result.value == "detailed"
    assert client.calls[0][1]["json"]["options"]["num_predict"] == 768


def _structured_post(client):
    async def post(path, **kwargs):
        client.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json={"message": {"content": '{"value":"detailed"}'}},
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )

    return post


@pytest.mark.asyncio
async def test_structured_retry_requests_compact_closed_json_after_truncation() -> None:
    client = RecordingClient()
    responses = iter(['{"value":"unfinished', '{"value":"complete"}'])

    async def post(path, **kwargs):
        client.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json={"message": {"content": next(responses)}},
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )

    client.post = post  # type: ignore[method-assign]
    provider = OllamaProvider(Settings(
        ollama_model="qwen3:latest",
        ollama_vision_model="qwen3-vl:8b",
        ollama_structured_max_attempts=2,
    ), client)  # type: ignore[arg-type]

    result = await provider.structured_output_bounded(
        [{"role": "user", "content": "analyze"}], TinyResult, max_output_tokens=768
    )

    assert result.value == "complete"
    retry_messages = client.calls[1][1]["json"]["messages"]
    assert "truncated JSON" in retry_messages[-1]["content"]


@pytest.mark.asyncio
async def test_vision_truncation_retry_escalates_output_budget() -> None:
    """A vision response cut at num_predict retries with a larger budget instead of
    deterministically re-truncating at the same spot (the reported visual-analysis
    failure: EOF inside a JSON string, repeated on every attempt)."""
    client = RecordingClient()
    responses = iter([
        {
            "done_reason": "length",
            "eval_count": 512,
            "message": {"content": '{"value":"unfinished'},
        },
        {"message": {"content": '{"value":"complete"}'}},
    ])

    async def post(path, **kwargs):
        client.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json=next(responses),
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )

    client.post = post  # type: ignore[method-assign]
    provider = OllamaProvider(Settings(
        ollama_model="qwen3:latest",
        ollama_vision_model="qwen3-vl:8b",
        ollama_vision_num_predict=512,
        ollama_structured_max_attempts=2,
    ), client)  # type: ignore[arg-type]

    result = await provider.structured_output_with_images(
        [{"role": "user", "content": "analyze the figure"}],
        [b"fake-image-bytes"],
        TinyResult,
    )

    assert result.value == "complete"
    assert client.calls[0][1]["json"]["options"]["num_predict"] == 512
    assert client.calls[1][1]["json"]["options"]["num_predict"] == 1024
    retry_messages = client.calls[1][1]["json"]["messages"]
    assert "truncated JSON" in retry_messages[-1]["content"]
    assert "increased" in retry_messages[-1]["content"]


@pytest.mark.asyncio
async def test_invalid_but_complete_json_retry_keeps_budget() -> None:
    """Schema-invalid but *complete* JSON (e.g. enum violation) is not an output
    truncation, so the retry keeps the same budget and just asks for compact JSON."""
    client = RecordingClient()
    responses = iter([
        {"done_reason": "stop", "eval_count": 40,
         "message": {"content": '{"value": 42}'}},
        {"done_reason": "stop", "eval_count": 30,
         "message": {"content": '{"value": "fixed"}'}},
    ])

    async def post(path, **kwargs):
        client.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json=next(responses),
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )

    client.post = post  # type: ignore[method-assign]
    provider = OllamaProvider(Settings(
        ollama_model="qwen3:latest",
        ollama_vision_model="qwen3-vl:8b",
        ollama_structured_max_attempts=2,
    ), client)  # type: ignore[arg-type]

    result = await provider.structured_output(
        [{"role": "user", "content": "analyze"}], TinyResult
    )

    assert result.value == "fixed"
    assert client.calls[0][1]["json"]["options"]["num_predict"] == 1536
    assert client.calls[1][1]["json"]["options"]["num_predict"] == 1536
