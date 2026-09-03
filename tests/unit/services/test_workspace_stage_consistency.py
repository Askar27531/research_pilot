"""Guard against `user_stage` values that response models cannot serialize.

`WorkspaceService.stage()` translates persistence stages (for example
``transfer_approval``, ``experiment_design``) into user-facing stages.  Every
value it can emit must be accepted by the ``user_stage`` Literal of
``ProjectSummary`` and ``ProjectWorkspace``; otherwise any project that
reaches a later approval stage turns every project/workspace read into a
response-validation 500.
"""

from types import SimpleNamespace
from typing import get_args

from app.schemas.workspace import ProjectSummary, ProjectWorkspace
from app.services.workspace import WorkspaceService

ALLOWED = (
    set(get_args(ProjectSummary.model_fields["user_stage"].annotation))
    | set(get_args(ProjectWorkspace.model_fields["user_stage"].annotation))
)


def _project(status: str = "waiting", current_stage: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, current_stage=current_stage, error=None)


def _job(status: str, job_type: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, job_type=job_type)


def test_running_job_stages_are_serializable() -> None:
    research_running = _job("running", "research")
    document_running = _job("running", "document_analysis")
    cases = [
        (_project(status="created"), None),
        (_project("waiting", "paper_selection"), None),
        (_project("waiting", "acquiring_selected"), None),
        (_project("waiting", "analyzing_selected"), None),
        (_project("waiting", "analysis_review"), None),
        (_project("waiting", "awaiting_documents"), None),
        (_project("waiting", "transfer_approval"), None),
        (_project("waiting", "experiment_design"), None),
        (_project("waiting", "experiment_approval"), None),
        (_project("waiting", "human_approval"), None),
        (_project("failed"), None),
        (_project("completed"), None),
        (_project("running", "analyzing_selected"), research_running),
        (_project("running", "analyzing_selected"), document_running),
        (_project("running", "paper_selection"), document_running),
    ]
    for project, job in cases:
        user_stage, label, detail = WorkspaceService.stage(project, job)
        assert user_stage in ALLOWED, (
            f"stage() emitted user_stage {user_stage!r} "
            f"(label={label!r}, detail={detail!r}) which no response model accepts"
        )
