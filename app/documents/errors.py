class DocumentError(RuntimeError):
    pass


class DocumentSecurityError(DocumentError):
    pass


class DocumentValidationError(DocumentError):
    pass


class DocumentNotFoundError(DocumentError):
    pass


class DocumentParseError(DocumentError):
    pass
