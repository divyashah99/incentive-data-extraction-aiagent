import logging
import urllib3
from urllib3.util.retry import Retry
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, RawContent

logger = logging.getLogger(__name__)

# We deliberately fall back to verify=False on broken cert chains (see fetch()).
# Suppress the resulting urllib3 warning so it doesn't spam every retry log line.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BOILERPLATE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript"]


class StaticScraper(BaseScraper):

    def __init__(self, rate_limit_seconds: float = 1.0, timeout: int = 30, max_retries: int = 3):
        super().__init__(rate_limit_seconds, timeout, max_retries)
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=self.max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(self._get_headers())
        return session

    def fetch(self, source: dict) -> RawContent:
        url = source.get("url", "")
        source_id = source.get("id", "unknown")
        self.rate_limit_seconds = source.get("rate_limit_seconds", self.rate_limit_seconds)

        self._rate_limit()
        try:
            try:
                response = self._session.get(url, timeout=self.timeout)
            except SSLError as ssl_exc:
                # Some servers (e.g. floridahousing.org) ship an incomplete cert
                # chain. Retry once with verification disabled so we can still scrape
                # public marketing content. We are NOT submitting credentials, so the
                # security impact is limited to MITM tampering with public HTML.
                logger.warning(
                    "SSL verify failed for %s (%s) — retrying with verify=False",
                    url, ssl_exc.__class__.__name__,
                )
                response = self._session.get(url, timeout=self.timeout, verify=False)
            response.raise_for_status()
            raw_html = response.text
            raw_text = self._clean_html(raw_html)
            logger.info("Fetched %s (%d chars text, %d chars html)", url, len(raw_text), len(raw_html))
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="html",
                raw_text=raw_text,
                raw_html=raw_html,   # always kept — table parser needs this
            )
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="html",
                success=False,
                error_message=str(exc),
            )

    def _clean_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(_BOILERPLATE_TAGS):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
