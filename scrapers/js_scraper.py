import asyncio
import logging
import os
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, RawContent

logger = logging.getLogger(__name__)

_BOILERPLATE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript"]


class JsScraper(BaseScraper):

    def fetch(self, source: dict) -> RawContent:
        return asyncio.run(self._async_fetch(source))

    async def _async_fetch(self, source: dict) -> RawContent:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            msg = "playwright not installed. Run: pip install playwright && playwright install chromium"
            logger.error(msg)
            return RawContent(
                source_id=source.get("id", "unknown"),
                source_url=source.get("url", ""),
                content_type="html",
                success=False,
                error_message=msg,
            )

        url = source.get("url", "")
        source_id = source.get("id", "unknown")
        wait_selector = source.get("js_wait_selector")
        extra_wait = float(source.get("js_wait_seconds", 0))  # extra settle time for slow SPAs
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

        # Some SPAs (e.g. Duke Energy) keep polling analytics endpoints, so
        # `networkidle` never fires. Default to `domcontentloaded` and let sources
        # opt-in to stricter waits via js_wait_until.
        wait_until = source.get("js_wait_until", "domcontentloaded")

        self._rate_limit()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                page = await browser.new_page(
                    user_agent=self.USER_AGENT,
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                try:
                    await page.goto(url, wait_until=wait_until, timeout=60_000)
                except Exception as goto_exc:
                    # Some sites never reach the desired wait state but the DOM is
                    # already populated by then. Fall back to a plain commit so we
                    # can still extract whatever loaded before the timeout.
                    logger.warning(
                        "page.goto(%s) wait_until=%s timed out — retrying with 'commit': %s",
                        url, wait_until, goto_exc,
                    )
                    await page.goto(url, wait_until="commit", timeout=30_000)

                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=15_000)
                    except Exception:
                        logger.warning("Selector '%s' not found on %s — continuing anyway", wait_selector, url)

                # Extra settle time for SPAs that load data after the initial wait
                if extra_wait > 0:
                    await page.wait_for_timeout(int(extra_wait * 1000))
                else:
                    # Tiny default settle so dynamic content has a chance to render
                    await page.wait_for_timeout(1500)

                html = await page.content()
                await browser.close()

            raw_text = self._clean_html(html)
            logger.info("JS-fetched %s (%d chars)", url, len(raw_text))
            return RawContent(
                source_id=source_id,
                source_url=url,
                content_type="html",
                raw_text=raw_text,
                raw_html=html,
            )
        except Exception as exc:
            logger.warning("JS fetch failed for %s: %s", url, exc)
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
