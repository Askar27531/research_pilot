"""Per-paper PDF acquisition models for the main workflow."""

from typing import Literal

from pydantic import BaseModel


class PaperAcquisition(BaseModel):
    project_id: str
    paper_id: str
    status: Literal["pending", "downloading", "awaiting_upload", "parsed", "failed"]
    source_url: str | None = None
    document_id: str | None = None
    error: str | None = None
    updated_at: str
