class InjectedFailure(RuntimeError):
    pass


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
