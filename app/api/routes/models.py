from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.dependencies import get_llm_provider
from app.core.config import get_settings
from app.llm import LLMProvider

router = APIRouter(prefix="/models", tags=["models"])


class ModelTestRequest(BaseModel):
    prompt: str = Field(
        default="Confirm that the language model is available.",
        min_length=1,
        max_length=1_000,
    )


class ModelProbe(BaseModel):
    status: Literal["ok"]
    response: str


class ModelTestResponse(BaseModel):
    status: Literal["ok"]
    provider: Literal["ollama"]
    model: str
    response: str


@router.post("/test", response_model=ModelTestResponse)
async def test_model(
    request: ModelTestRequest,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
) -> ModelTestResponse:
    result = await provider.structured_output(
        [
            {
                "role": "system",
                "content": "You are a connectivity probe. Return status 'ok' and a concise response.",
            },
            {"role": "user", "content": request.prompt},
        ],
        ModelProbe,
    )

    return ModelTestResponse(
        status="ok",
        provider="ollama",
        model=get_settings().ollama_model,
        response=result.response,
    )
