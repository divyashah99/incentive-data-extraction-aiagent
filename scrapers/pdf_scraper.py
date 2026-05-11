import io
import logging
from urllib3.util.retry import Retry
import requests
from requests.adapters import HTTPAdapter

from scrapers.base_scraper import BaseScraper, RawContent

logger = logging.getLogger(__name__)


class PdfScraper(BaseScraper):

    def __init__(self, rate_limit_seconds: float = 1.0, timeout: int = 30, max_retries: int = 3):
        super().__init__(rate_limit_seconds, timeout, max_retries)
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=self.max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.headers.update(self._get_headers())
        return session

    def fetch(self, source: dict) -> RawContent:
        url = source.get("url", "")
        source_id = source.get("id", "unknown")
        rate_limit = source.get("rate_limit_seconds", self.rate_limit_seconds)
        self.rate_limit_seconds = rate_limit

        self._rate_limit()
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return self.fetch_from_bytes(response.content, source_id, url)
        except Exception as exc:
            logger.warning("PDF download failed for %s: %s", url, exc)
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="pdf",
                success=False,
                error_message=str(exc),
            )

    def fetch_from_bytes(self, pdf_bytes: bytes, source_id: str, url: str) -> RawContent:
        try:
            import pdfplumber
        except ImportError:
            msg = "pdfplumber not installed. Run: pip install pdfplumber"
            logger.error(msg)
            return RawContent(source_id=source_id, source_url=url, content_type="pdf", success=False, error_message=msg)

        try:
            pages_text: list[str] = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
            raw_text = "\n\n".join(pages_text)
            logger.info("Extracted %d chars from PDF at %s", len(raw_text), url)
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="pdf",
                raw_text=raw_text,
            )
        except Exception as exc:
            logger.warning("PDF parse failed for %s: %s", url, exc)
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="pdf",
                success=False,
                error_message=str(exc),
            )
