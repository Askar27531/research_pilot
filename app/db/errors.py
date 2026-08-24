class RepositoryError(RuntimeError):
    pass


class RecordNotFoundError(RepositoryError):
    pass


class ProjectConflictError(RepositoryError):
    pass


class EvidenceReferencedError(RepositoryError):
    pass
