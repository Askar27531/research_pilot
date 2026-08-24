import logging


def configure_logging() -> None:
    """Configure concise application logs without logging request bodies."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTP request URLs may contain credentials such as OpenAlex's api_key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
