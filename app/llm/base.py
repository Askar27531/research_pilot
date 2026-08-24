from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TypeVar

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

    @abstractmethod
    async def close(self) -> None:
        """Release resources held by the provider."""
