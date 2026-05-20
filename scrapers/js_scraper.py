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
        expand_accordions = bool(source.get("js_expand_accordions", False))
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

                # Optionally expand collapsed/accordion content so all text becomes
                # visible to the extractor. Used for Power Pages-style portals where
                # award tiers, eligibility, FAQs etc. live in collapsed panes.
                if expand_accordions:
                    await self._expand_accordions(page, url)

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

    async def _expand_accordions(self, page, url: str) -> None:
        """
        Force-open all collapsed/accordion content on the page so its text is
        captured by page.content(). Targets the patterns Power Pages, Bootstrap,
        and most CMS templates use:

          • <details>                  — set the `open` attribute
          • .collapse                  — add `.show` class (Bootstrap 4/5 collapse)
          • [aria-expanded="false"]    — flip to "true" + click the control
          • [aria-hidden="true"]       — flip to "false" on content panes
          • Buttons with text matching "show more", "read more", "expand", "learn more"

        Implemented as one page.evaluate() call so we don't pay per-element click
        latency. Failures here are non-fatal — we still extract whatever rendered.
        """
        js = r"""
        () => {
            let opened = 0;

            // 1. Open all <details> elements
            document.querySelectorAll('details').forEach(d => {
                if (!d.open) { d.open = true; opened++; }
            });

            // 2. Force Bootstrap collapse panes open
            document.querySelectorAll('.collapse:not(.show)').forEach(el => {
                el.classList.add('show');
                el.style.display = 'block';
                opened++;
            });

            // 3. Flip aria-expanded and aria-hidden flags
            document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                el.setAttribute('aria-expanded', 'true');
                opened++;
            });
            document.querySelectorAll('[aria-hidden="true"]').forEach(el => {
                // Don't unhide globally-hidden chrome (modals, toasts) — only
                // unhide elements that look like content panes.
                if (el.matches('section, div, article, ul, ol, table, p, span')) {
                    el.setAttribute('aria-hidden', 'false');
                }
            });

            // 4. Click obvious "show more" / "expand" controls
            const expandRe = /\b(show more|read more|expand|learn more|view (more|all)|see (more|details))\b/i;
            document.querySelectorAll('button, a, [role="button"]').forEach(btn => {
                const txt = (btn.innerText || btn.textContent || '').trim();
                if (txt && expandRe.test(txt) && txt.length < 60) {
                    try { btn.click(); opened++; } catch (e) {}
                }
            });

            return opened;
        }
        """
        try:
            opened = await page.evaluate(js)
            # Small settle so any animations / lazy-loaded sub-content can render
            await page.wait_for_timeout(800)
            logger.info("Expanded %d collapsed element(s) on %s", opened, url)
        except Exception as exc:
            logger.warning("Accordion expansion failed on %s: %s", url, exc)

    def _clean_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(_BOILERPLATE_TAGS):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
