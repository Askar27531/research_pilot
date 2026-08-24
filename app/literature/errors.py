class LiteratureError(RuntimeError):
    retryable = False


class LiteratureRateLimitError(LiteratureError):
    retryable = True


class LiteratureUnavailableError(LiteratureError):
    retryable = True


class LiteratureResponseError(LiteratureError):
    retryable = False


class PaperNotFoundError(LiteratureError):
    retryable = False


class OpenAlexAuthRequiredError(LiteratureError):
    retryable = False
