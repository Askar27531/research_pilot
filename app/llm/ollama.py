import asyncio
import base64
import json
import logging
from collections.abc import Sequence
from typing import Any, Self

import httpx

from app.core.config import Settings, get_settings
from app.llm.base import LLMCallUsage, LLMError, LLMProvider, StructuredOutput, UsageSink

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Async Ollama provider using the native `/api/chat` endpoint."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.usage_sink = usage_sink
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
        return await self._validated_structured_request(
            messages, response_model, format_schema, error_label="structured output"
        )

    async def structured_output_bounded(
        self,
        messages: Sequence[dict[str, str]],
        response_model: type[StructuredOutput],
        *,
        max_output_tokens: int,
    ) -> StructuredOutput:
        format_schema = _ollama_compatible_schema(response_model.model_json_schema())
        return await self._validated_structured_request(
            messages,
            response_model,
            format_schema,
            error_label="structured output",
            max_output_tokens=max_output_tokens,
        )

    async def structured_output_with_images(
        self,
        messages: Sequence[dict[str, Any]],
        images: Sequence[bytes],
        response_model: type[StructuredOutput],
    ) -> StructuredOutput:
        if not self.settings.ollama_vision_model.strip():
            raise LLMError("OLLAMA_VISION_MODEL is not configured")
        enriched = [dict(message) for message in messages]
        if not enriched:
            enriched = [{"role": "user", "content": "Analyze the supplied image."}]
        enriched[-1]["images"] = [base64.b64encode(value).decode("ascii") for value in images]
        schema = _ollama_compatible_schema(response_model.model_json_schema())
        return await self._validated_structured_request(
            enriched,
            response_model,
            schema,
            model=self.settings.ollama_vision_model,
            error_label="visual analysis",
        )

    async def _validated_structured_request(
        self,
        messages: Sequence[dict[str, Any]],
        response_model: type[StructuredOutput],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        error_label: str,
        max_output_tokens: int | None = None,
    ) -> StructuredOutput:
        """Ask for schema-valid JSON with bounded, output-aware retries.

        When the model is *cut off* at its token budget (Ollama reports
        ``done_reason == "length"`` or the JSON simply stops mid-document), a plain
        retry regenerates the same truncated answer because ``num_predict`` is
        unchanged — that is why the previous retries kept failing on the same visual.
        Each retry therefore raises the output budget until the JSON can finish
        (up to a ceiling). An explicit ``max_output_tokens`` request budget is
        treated as a hard ceiling and is never exceeded.
        """
        last_error: ValueError | None = None
        effective_messages = list(messages)
        output_budget = (
            max_output_tokens
            if max_output_tokens is not None
            else self._default_output_budget(model)
        )
        budget_ceiling = (
            max_output_tokens
            if max_output_tokens is not None
            else self._output_budget_ceiling(model)
        )
        for attempt in range(self.settings.ollama_structured_max_attempts):
            data = await self._request(
                effective_messages,
                format_schema=schema,
                model=model,
                max_output_tokens=output_budget,
            )
            candidates = _structured_response_candidates(data)
            if not candidates:
                last_error = ValueError("Ollama returned empty content and thinking fields")
            for candidate in candidates:
                try:
                    return response_model.model_validate_json(candidate)
                except ValueError as exc:
                    last_error = exc
            if attempt + 1 >= self.settings.ollama_structured_max_attempts:
                continue
            truncated = self._response_was_truncated(
                data, output_budget, candidates
            )
            budget_raised = truncated and output_budget < budget_ceiling
            if budget_raised:
                output_budget = min(budget_ceiling, output_budget * 2)
            effective_messages = [
                *messages,
                {
                    "role": "system",
                    "content": (
                        _TRUNCATED_RETRY_INSTRUCTION
                        if budget_raised else _INVALID_RETRY_INSTRUCTION
                    ),
                },
            ]
            await asyncio.sleep(0.25 * (attempt + 1))
        raise LLMError(f"Ollama returned invalid {error_label}: {last_error}") from last_error

    def _default_output_budget(self, model: str | None) -> int:
        """Configured token budget for an unbounded structured request."""
        if model is not None:
            return self.settings.ollama_vision_num_predict
        return self.settings.ollama_num_predict

    @staticmethod
    def _output_budget_ceiling(model: str | None) -> int:
        """Ceiling for retry escalation; mirrors the Field(le=...) bounds on the
        corresponding settings knobs (ollama_vision_num_predict / ollama_num_predict)."""
        return 4096 if model is not None else 8192

    @staticmethod
    def _response_was_truncated(
        data: dict[str, Any],
        output_budget: int | None,
        candidates: Sequence[str],
    ) -> bool:
        """True when the model hit the output limit before the JSON could finish.

        Ollama reports ``done_reason == "length"`` and the number of generated
        tokens (``eval_count``) when generation stops at ``num_predict``. As a
        fallback (older Ollama builds), a candidate whose JSON stops mid-document
        is treated as truncated too.
        """
        if data.get("done_reason") == "length":
            return True
        eval_count = data.get("eval_count")
        if (
            isinstance(eval_count, int)
            and output_budget is not None
            and eval_count >= output_budget
        ):
            return True
        return any(_is_truncated_json(candidate) for candidate in candidates)

    async def _request(
        self,
        messages: Sequence[dict[str, Any]],
        format_schema: dict[str, Any] | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        is_vision = model is not None
        timeout_seconds = (
            self.settings.ollama_vision_timeout_seconds
            if is_vision else self.settings.ollama_timeout_seconds
        )
        payload: dict[str, Any] = {
            "model": model or self.settings.ollama_model,
            "messages": list(messages),
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "num_ctx": (
                    self.settings.ollama_vision_num_ctx
                    if is_vision else self.settings.ollama_num_ctx
                ),
                "num_predict": (
                    max_output_tokens
                    if max_output_tokens is not None
                    else (
                        self.settings.ollama_vision_num_predict
                        if is_vision else self.settings.ollama_num_predict
                    )
                ),
            },
        }
        if format_schema is not None:
            payload["format"] = format_schema

        try:
            response = await self._client.post(
                "/api/chat", json=payload, timeout=timeout_seconds
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama request timed out after {timeout_seconds}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise LLMError(f"Ollama returned HTTP {exc.response.status_code}: {detail}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise LLMError(f"Could not communicate with Ollama: {exc}") from exc

        if not isinstance(data, dict):
            raise LLMError("Ollama returned a non-object JSON response")
        await self._meter(data, payload["model"], is_vision)
        return data

    async def _meter(self, data: dict[str, Any], model: str, is_vision: bool) -> None:
        """Feed one successful HTTP call into the run's usage sink (best effort).

        Metered per actual response: an invalid-JSON attempt that triggered a
        retry already cost tokens and is counted as the real spend it is. A
        failing sink must never break (or slow down) the LLM call itself.
        """
        # Metering is observability, never a gate: a failing sink must not
        # break (or slow down) the LLM call that already succeeded.
        if self.usage_sink is None:
            return
        try:
            await self.usage_sink(LLMCallUsage(
                kind="vision" if is_vision else "text",
                model=str(data.get("model") or model),
                prompt_tokens=int(data.get("prompt_eval_count") or 0),
                completion_tokens=int(data.get("eval_count") or 0),
                latency_ms=None,
            ))
        except Exception:
            logger.warning("usage sink failed for model=%s", model, exc_info=True)

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


def _structured_response_candidates(data: dict[str, Any]) -> list[str]:
    """Return likely JSON values, including Qwen's occasional thinking-only result."""
    message = data.get("message")
    if not isinstance(message, dict):
        return []
    candidates: list[str] = []
    for key in ("content", "thinking"):
        value = message.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        stripped = value.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]).strip()
        candidates.append(stripped)
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start and stripped[start:end + 1] != stripped:
            candidates.append(stripped[start:end + 1])
    return list(dict.fromkeys(candidates))


def _is_truncated_json(candidate: str) -> bool:
    """True when *candidate* is JSON that stops mid-document (cut by an output limit).

    Completion heuristics: a string that never closes, or a parse error at the very
    end of the input, both mean the document was cut before it could finish.
    """
    try:
        json.loads(candidate)
        return False
    except json.JSONDecodeError as exc:
        if exc.msg.startswith("Unterminated string"):
            return True
        return exc.pos >= len(candidate.rstrip()) - 1


_INVALID_RETRY_INSTRUCTION = (
    "The previous response was invalid or truncated JSON. Return only a compact "
    "JSON object matching the schema. Shorten prose where necessary and always "
    "close every string, array, and object before the output limit."
)

_TRUNCATED_RETRY_INSTRUCTION = (
    "The previous response was truncated JSON: it was cut off by the output limit "
    "before the JSON was complete. The output budget has been increased, so now "
    "finish the complete JSON object matching the schema. Be concise but do not "
    "omit required fields, and close every string, array, and object."
)
