from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = Field(min_length=1)
    ollama_timeout_seconds: float = Field(default=300, gt=0)
    ollama_vision_model: str = ""
    ollama_vision_timeout_seconds: float = Field(default=300, gt=0)
    ollama_structured_max_attempts: int = Field(default=2, ge=1, le=5)
    ollama_num_ctx: int = Field(default=16384, ge=2048, le=32768)
    ollama_num_predict: int = Field(default=1536, ge=128, le=8192)
    ollama_vision_num_ctx: int = Field(default=8192, ge=2048, le=32768)
    ollama_vision_num_predict: int = Field(default=512, ge=64, le=4096)
    visual_analysis_concurrency: int = Field(default=2, ge=1, le=4)
    paper_analysis_concurrency: int = Field(default=2, ge=1, le=2)
    paper_analysis_section_tokens: int = Field(default=4096, ge=512, le=4096)
    paper_analysis_experiment_tokens: int = Field(default=1536, ge=768, le=2048)
    paper_analysis_overview_tokens: int = Field(default=768, ge=384, le=1536)
    ocr_languages: str = "eng"
    pdf_render_dpi: int = Field(default=200, ge=72, le=300)
    resource_token_secret: str = Field(default="research-pilot-local-resource-key", min_length=16)

    openalex_base_url: str = "https://api.openalex.org"
    openalex_api_key: str | None = None
    openalex_timeout_seconds: float = Field(default=30, gt=0)
    openalex_max_attempts: int = Field(default=3, ge=1, le=5)
    openalex_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    crossref_base_url: str = "https://api.crossref.org"
    arxiv_base_url: str = "https://export.arxiv.org/api/query"
    literature_timeout_seconds: float = Field(default=30, gt=0)
    literature_cache_ttl_seconds: float = Field(default=900, gt=0)

    # 排序口径：被 LLM 精筛判为 include 的论文按语义相关分优先排序，词法分仅作为
    # 未进入精筛名单论文的兜底。以下权重字段保留仅为旧配置兼容，不再参与计算。
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
    mcp_config_path: str = "config/mcp_servers.json"
    mcp_allowed_roots: list[str] = Field(default_factory=lambda: [str(Path.cwd())])


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
