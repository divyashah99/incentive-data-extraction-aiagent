from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import logging

logger = logging.getLogger(__name__)


@dataclass
class RawContent:
    source_id: str
    source_url: str
    content_type: str              # "html", "pdf", "json", "structured"
    raw_text: str = ""
    raw_html: str | None = None
    fetch_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    success: bool = True
    error_message: str | None = None
    # Optional: pre-parsed records that skip LLM entirely (e.g. DSIRE direct mapping)
    parsed_records: list[dict] = field(default_factory=list)


class BaseScraper(ABC):
    USER_AGENT = "DreamlineAI-IncentiveCrawler/1.0 (research; contact: admin@dreamlineai.org)"

    def __init__(
        self,
        rate_limit_seconds: float = 1.0,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_time: float = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        wait = self.rate_limit_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    def _get_headers(self) -> dict:
        return {"User-Agent": self.USER_AGENT}

    @abstractmethod
    def fetch(self, source: dict) -> RawContent:
        """Fetch content from a source config dict. Always returns RawContent, never raises."""
