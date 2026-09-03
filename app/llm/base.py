from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


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
