"""
Discoverer — loads sources.yaml, filters by status (spec §9.5), and returns
the correct scraper instance for each source.

Spec §9.5: the registry is an open set sorted by operational needs (stale
fetch, quarantine rate), not a static priority ranking. There is no P0/P1/P2.
"""
import logging
from pathlib import Path

import yaml

from scrapers.base_scraper import BaseScraper
from scrapers.static_scraper import StaticScraper
from scrapers.api_scraper import ApiScraper
from scrapers.js_scraper import JsScraper
from scrapers.pdf_scraper import PdfScraper

logger = logging.getLogger(__name__)


class Discoverer:

    def __init__(self, config_path: str = "config/sources.yaml"):
        self._config_path = Path(config_path)
        self._config: dict = {}

    def load_sources(
        self,
        source_filter: str | None = None,
        group_filter: str | None = None,
    ) -> list[dict]:
        """
        Load sources from YAML, keeping only ``status: active`` entries
        unless ``source_filter`` explicitly names one (so candidate / deprecated
        sources can still be tested by id).
        """
        if not self._config_path.exists():
            raise FileNotFoundError(f"sources.yaml not found at {self._config_path.resolve()}")

        with self._config_path.open("r", encoding="utf-8") as fh:
            self._config = yaml.safe_load(fh)

        all_sources: list[dict] = self._config.get("sources", [])

        if source_filter:
            # Allow testing any source by id regardless of status
            sources = [s for s in all_sources if s.get("id") == source_filter]
        else:
            sources = [s for s in all_sources if s.get("status", "active") == "active"]
            if group_filter:
                sources = [
                    s for s in sources
                    if s.get("sheet_group", "").lower() == group_filter.lower()
                ]

        logger.info("Loaded %d sources from %s", len(sources), self._config_path)
        return sources

    def get_scraper_for_source(self, source: dict) -> BaseScraper:
        """Factory: return the correct scraper based on source type."""
        scraper_type = source.get("type", "static_html")
        rate_limit = source.get("rate_limit_seconds", 1.0)

        if scraper_type == "api":
            return ApiScraper(rate_limit_seconds=rate_limit)
        elif scraper_type == "js_rendered":
            return JsScraper(rate_limit_seconds=rate_limit)
        elif scraper_type == "pdf":
            return PdfScraper(rate_limit_seconds=rate_limit)
        else:  # static_html (default)
            return StaticScraper(rate_limit_seconds=rate_limit)

    @property
    def metadata(self) -> dict:
        return self._config.get("metadata", {})
