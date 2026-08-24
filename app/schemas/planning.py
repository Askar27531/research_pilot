from pydantic import BaseModel, Field, field_validator


class ResearchUnderstanding(BaseModel):
    """Structured interpretation produced before research begins."""

    normalized_goal: str = Field(min_length=3, max_length=4_000)
    core_concepts: list[str] = Field(min_length=1, max_length=20)
    domain: str = Field(min_length=2, max_length=200)
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)
    ambiguities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("normalized_goal", "domain")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("core_concepts", "ambiguities")
    @classmethod
    def normalize_items(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            key = value.casefold()
            if value and key not in seen:
                result.append(value)
                seen.add(key)
        return result
