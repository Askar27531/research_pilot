from collections.abc import Sequence
from typing import Any, Self

import httpx

from app.core.config import Settings, get_settings
from app.llm.base import LLMError, LLMProvider, StructuredOutput


class OllamaProvider(LLMProvider):
    """Async Ollama provider using the native `/api/chat` endpoint."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.ollama_base_url.rstrip("/"),
            timeout=self.settings.ollama_timeout_seconds,
        )

    async def chat(self, messages: Sequence[dict[str, str]]) -> str:
        data = await self._request(messages)
        content = data.get("message", {}).get("content")
        if not isinstance(content, str):
            raise LLMError("Ollama response did not contain message.content")
        return content

    async def structured_output(
        self,
        messages: Sequence[dict[str, str]],
        response_model: type[StructuredOutput],
    ) -> StructuredOutput:
        format_schema = _ollama_compatible_schema(response_model.model_json_schema())
        data = await self._request(messages, format_schema=format_schema)
        content = data.get("message", {}).get("content")
        if not isinstance(content, str):
            raise LLMError("Ollama structured response did not contain message.content")
        try:
            return response_model.model_validate_json(content)
        except ValueError as exc:
            raise LLMError(f"Ollama returned invalid structured output: {exc}") from exc

    async def _request(
        self,
        messages: Sequence[dict[str, str]],
        format_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "messages": list(messages),
            "stream": False,
            "think": False,
        }
        if format_schema is not None:
            payload["format"] = format_schema

        try:
            response = await self._client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama request timed out after {self.settings.ollama_timeout_seconds}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise LLMError(f"Ollama returned HTTP {exc.response.status_code}: {detail}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise LLMError(f"Could not communicate with Ollama: {exc}") from exc

        if not isinstance(data, dict):
            raise LLMError("Ollama returned a non-object JSON response")
        return data

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def _ollama_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove validation annotations that Ollama's grammar parser does not need.

    Pydantic still performs the complete validation after generation. Keeping the
    generation schema structural avoids grammar initialization failures caused by
    large string/array bounds in some Ollama versions.
    """

    structural_keys = {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "enum",
        "items",
        "oneOf",
        "properties",
        "required",
        "type",
    }

    def clean(value: Any, parent_key: str | None = None) -> Any:
        if isinstance(value, dict):
            if parent_key in {"$defs", "properties"}:
                return {key: clean(item) for key, item in value.items()}
            return {key: clean(item, key) for key, item in value.items() if key in structural_keys}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = clean(schema)
    if not isinstance(result, dict):
        raise LLMError("Could not create an Ollama-compatible JSON schema")
    return result
