class InjectedFailure(RuntimeError):
    pass


class AnalysisPausedError(RuntimeError):
    """Raised at a safe boundary to stop a long-running analysis job.

    Two reasons share the same safe-boundary machinery:
    - ``reason="user"``: the human requested a cooperative pause.
    - ``reason="budget_gate"``: cumulative LLM usage crossed a cost threshold
      (see ResearchDataRepository.budget_pause_reason / budget_gate_* settings).

    The workflow service catches it, parks the project at ``analysis_paused``
    (recording ``projects.pause_reason``) and lets the job finish without a
    failure; all persisted units are intact and a later resume reuses them
    (work items / document analyses / paper analyses are idempotent).
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or "user"


class FailureInjector:
    """Deterministic test harness; never enabled by production configuration."""

    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.seen = 0
        self.triggered = False

    def before_item(self) -> None:
        self.seen += 1
        if self.seen == self.fail_at and not self.triggered:
            self.triggered = True
            raise InjectedFailure(f"Injected failure at item {self.fail_at}")
