from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ResearchRequest(BaseModel):
    """Validated user input for starting a research workflow."""

    research_question: str = Field(min_length=3, max_length=4_000)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)
    maximum_papers: int = Field(default=15, ge=1, le=100)
    existing_files: list[str] = Field(default_factory=list, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    literature_sources: list[Literal["openalex", "crossref", "arxiv"]] = Field(
        default_factory=lambda: ["openalex", "crossref", "arxiv"]
    )

    @field_validator("research_question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("research_question must contain at least 3 non-whitespace characters")
        return value

    @field_validator("keywords", "constraints")
    @classmethod
    def normalize_unique_text_items(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            key = value.casefold()
            if value and key not in seen:
                normalized.append(value)
                seen.add(key)
        return normalized

    @field_validator("existing_files")
    @classmethod
    def normalize_file_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = str(Path(raw_value.strip())) if raw_value.strip() else ""
            key = value.casefold()
            if value and key not in seen:
                normalized.append(value)
                seen.add(key)
        return normalized

    @model_validator(mode="after")
    def validate_year_range(self) -> "ResearchRequest":
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must be less than or equal to year_to")
        return self
