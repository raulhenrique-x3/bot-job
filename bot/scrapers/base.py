import logging

logger = logging.getLogger("jobbot")


class JobSource:
    """Base class for portal scrapers."""

    name: str = "base"

    def scrape(self) -> list[dict]:
        raise NotImplementedError
