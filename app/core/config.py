from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = Field(min_length=1)
    ollama_timeout_seconds: float = Field(default=300, gt=0)

    openalex_base_url: str = "https://api.openalex.org"
    openalex_api_key: str | None = None
    openalex_timeout_seconds: float = Field(default=30, gt=0)
    openalex_max_attempts: int = Field(default=3, ge=1, le=5)
    openalex_backoff_seconds: float = Field(default=0.5, ge=0, le=10)

    ranking_lexical_weight: float = Field(default=0.45, ge=0, le=1)
    ranking_llm_weight: float = Field(default=0.55, ge=0, le=1)
    ranking_llm_top_n: int = Field(default=10, ge=1, le=30)
    ranking_abstract_chars: int = Field(default=600, ge=100, le=2_000)

    database_path: str = "data/research_pilot.db"
    skills_root: str = "skills"
    skill_max_bytes: int = Field(default=64_000, ge=1_000, le=1_000_000)
    workspace_root: str = "data/workspaces"
    document_max_bytes: int = Field(default=50_000_000, ge=1_000_000)
    document_render_dpi: int = Field(default=144, ge=72, le=300)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
