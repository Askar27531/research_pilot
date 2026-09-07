from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


@dataclass(frozen=True)
class LLMCallUsage:
    """One successful provider call, as observed at the HTTP response level.

    ``prompt_tokens`` / ``completion_tokens`` come from Ollama's
    ``prompt_eval_count`` / ``eval_count``; retried attempts are metered as the
    real spend they are (each invalid-but-completed response already cost tokens).
    """

    kind: str  # "text" | "vision"
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int | None = None


#: Async sink receiving one row per successful provider call. The receiver owns
#: the run context (project_id / phase); metering failures must never surface.
UsageSink = Callable[[LLMCallUsage], Awaitable[None]]


class LLMError(RuntimeError):
    """Raised when an LLM provider cannot complete a request."""


class LLMProvider(ABC):
    """Provider-independent interface for text and structured generation."""

    @abstractmethod
    async def chat(self, messages: Sequence[dict[str, str]]) -> str:
        """Return the assistant text for a chat conversation."""

    @abstractmethod
    async def structured_output(
        self,
        messages: Sequence[dict[str, str]],
        response_model: type[StructuredOutput],
    ) -> StructuredOutput:
        """Return a response validated against a Pydantic model."""

    async def structured_output_bounded(
        self,
        messages: Sequence[dict[str, str]],
        response_model: type[StructuredOutput],
        *,
        max_output_tokens: int,
    ) -> StructuredOutput:
        """Return structured output with a request-specific output budget when supported."""
        return await self.structured_output(messages, response_model)

    async def structured_output_with_images(
        self,
        messages: Sequence[dict[str, Any]],
        images: Sequence[bytes],
        response_model: type[StructuredOutput],
    ) -> StructuredOutput:
        """Return structured output grounded in one or more images."""
        raise LLMError("This provider does not support image input")

    @abstractmethod
    async def close(self) -> None:
        """Release resources held by the provider."""
