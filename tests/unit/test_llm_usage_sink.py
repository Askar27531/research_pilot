"""Provider-level usage metering: every successful Ollama HTTP call (text or
vision, including retried attempts) is fed to the configured usage sink."""

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.llm.base import LLMCallUsage
from app.llm.ollama import OllamaProvider


class RecordingClient:
    def __init__(self, responses) -> None:
        self.calls = []
        self.responses = responses

    async def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json=self.responses.pop(0),
            request=httpx.Request("POST", f"http://ollama.test{path}"),
        )


class TinyResult(BaseModel):
    value: str


def _collector() -> tuple[list[LLMCallUsage], object]:
    recorded: list[LLMCallUsage] = []

    async def sink(usage: LLMCallUsage) -> None:
        recorded.append(usage)

    return recorded, sink


def _usage_response(content: str, prompt: int = 12, completion: int = 5) -> dict:
    return {
        "model": "qwen:metered",
        "prompt_eval_count": prompt,
        "eval_count": completion,
        "message": {"content": content},
    }


def _settings() -> Settings:
    return Settings(
        ollama_model="qwen:metered",
        ollama_vision_model="qwen-vl:metered",
        ollama_structured_max_attempts=2,
    )


@pytest.mark.asyncio
async def test_chat_call_feeds_usage_sink() -> None:
    recorded, sink = _collector()
    client = RecordingClient([
        _usage_response("ok", prompt=20, completion=7),
    ])
    provider = OllamaProvider(_settings(), client, usage_sink=sink)  # type: ignore[arg-type]

    assert await provider.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert len(recorded) == 1
    assert recorded[0].kind == "text"
    assert recorded[0].prompt_tokens == 20
    assert recorded[0].completion_tokens == 7
    assert recorded[0].model == "qwen:metered"


@pytest.mark.asyncio
async def test_vision_call_is_metered_as_vision() -> None:
    recorded, sink = _collector()
    client = RecordingClient([
        _usage_response('{"value":"seen"}', prompt=400, completion=90),
    ])
    provider = OllamaProvider(_settings(), client, usage_sink=sink)  # type: ignore[arg-type]

    result = await provider.structured_output_with_images(
        [{"role": "user", "content": "inspect"}], [b"image"], TinyResult
    )
    assert result.value == "seen"
    assert len(recorded) == 1
    assert recorded[0].kind == "vision"
    assert recorded[0].prompt_tokens == 400
    assert recorded[0].completion_tokens == 90


@pytest.mark.asyncio
async def test_retried_attempts_are_all_metered() -> None:
    """A schema-invalid first response already cost tokens; the metered spend
    must include it (the M3 ledger counts real spend, not just successes)."""
    recorded, sink = _collector()
    client = RecordingClient([
        _usage_response('{"value": 42}', prompt=15, completion=6),
        _usage_response('{"value":"fixed"}', prompt=25, completion=4),
    ])
    provider = OllamaProvider(_settings(), client, usage_sink=sink)  # type: ignore[arg-type]

    result = await provider.structured_output(
        [{"role": "user", "content": "analyze"}], TinyResult
    )
    assert result.value == "fixed"
    assert len(recorded) == 2
    assert [item.kind for item in recorded] == ["text", "text"]
    assert recorded[0].prompt_tokens == 15
    assert recorded[1].prompt_tokens == 25


@pytest.mark.asyncio
async def test_failing_sink_never_breaks_the_call() -> None:
    async def explode(usage: LLMCallUsage) -> None:  # sink backend fault
        raise RuntimeError("metering backend down")

    client = RecordingClient([_usage_response("ok")])
    provider = OllamaProvider(_settings(), client, usage_sink=explode)  # type: ignore[arg-type]

    assert await provider.chat([{"role": "user", "content": "hi"}]) == "ok"
