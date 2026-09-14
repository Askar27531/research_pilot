from functools import lru_cache

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
    # Vision output budget: a VisualObservation JSON (summary + several lists)
    # regularly needs >512 tokens; too small a budget truncates mid-string and
    # the model deterministically re-truncates on retry. Kept within 4096.
    ollama_vision_num_predict: int = Field(default=2048, ge=64, le=4096)
    visual_analysis_concurrency: int = Field(default=2, ge=1, le=4)
    # P2-a: when enabled, prose references that point to figures the deterministic
    # parser could not localize are analyzed at page level (vision reads the full
    # page render). Off by default: the repair costs extra vision calls.
    figure_repair_enabled: bool = False
    # M2: scan prose-mention vs figure/table consistency for every visual the
    # paper text mentions (not only ones cited by analysis claims). Each such
    # visual costs one extra vision call.
    cross_modal_consistency_enabled: bool = True
    paper_analysis_concurrency: int = Field(default=2, ge=1, le=2)
    paper_analysis_section_tokens: int = Field(default=4096, ge=512, le=4096)
    paper_analysis_experiment_tokens: int = Field(default=1536, ge=768, le=2048)
    paper_analysis_overview_tokens: int = Field(default=768, ge=384, le=1536)
    # Display-level budget hints (decision desk lean plan): the analysis stage
    # shows a yellow reminder once the cumulative visual count or elapsed wall
    # time crosses these thresholds. Purely advisory - no metering, no pause.
    budget_warning_vision_calls: int = Field(default=40, ge=1, le=10_000)
    budget_warning_minutes: int = Field(default=30, ge=1, le=24 * 60)
    # M3 cost-threshold pause (hard supervisory point, unlike the hints above):
    # every successful LLM call is metered into `llm_usage`; once a *window* of
    # usage crosses any threshold below, the analysis parks at the next safe
    # boundary (pause_reason='budget_gate') until a human decides to continue.
    # "Continue" resets the window baseline, so a run asks again after each
    # additional threshold-sized block of spend instead of nagging every step.
    budget_gate_enabled: bool = True
    budget_gate_tokens: int = Field(default=600_000, ge=1_000, le=100_000_000)
    budget_gate_vision_calls: int = Field(default=60, ge=1, le=100_000)
    budget_gate_minutes: int = Field(default=45, ge=1, le=24 * 60)
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
    # 未进入精筛名单论文的兜底。
    ranking_llm_top_n: int = Field(default=10, ge=1, le=30)
    ranking_abstract_chars: int = Field(default=600, ge=100, le=2_000)

    database_path: str = "data/research_pilot.db"
    skills_root: str = "skills"
    skill_max_bytes: int = Field(default=64_000, ge=1_000, le=1_000_000)
    workspace_root: str = "data/workspaces"
    document_max_bytes: int = Field(default=50_000_000, ge=1_000_000)
    mcp_config_path: str = "config/mcp_servers.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
