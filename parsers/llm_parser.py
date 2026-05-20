"""
LLM Parser — uses LangChain with Groq or Gemini (both free) for structured extraction.

Provider is selected via LLM_PROVIDER env var:
  LLM_PROVIDER=groq    → Groq (default) — llama-3.3-70b-versatile, very fast
  LLM_PROVIDER=gemini  → Google Gemini 1.5 Flash, good instruction following

Monitoring via LangSmith — set LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY.
All LLM calls are auto-traced with zero extra code.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from scrapers.base_scraper import RawContent

load_dotenv()

logger = logging.getLogger(__name__)

# ── LangSmith monitoring is enabled automatically when these env vars are set:
#    LANGCHAIN_TRACING_V2=true
#    LANGCHAIN_API_KEY=<your-key>
#    LANGCHAIN_PROJECT=dreamline-incentive-extraction
# No code changes needed — LangChain auto-instruments all LLM calls.

# ── Pydantic models for structured LLM output ─────────────────────────────────

VALID_INCENTIVE_TYPES = Literal[
    "Grants", "Rebates", "Finance Solutions", "Tax Credits", "Investments"
]


class IncentiveExtraction(BaseModel):
    """One incentive program extracted from source content."""
    program_name: str | None = Field(None, description="Official name of the incentive program.")
    state: str | None = Field(None, description="State name, e.g. 'Florida'. Null if unclear.")
    city: str | None = Field(
        None,
        description="'Tampa' for city programs, 'Hillsborough County' for county-wide, null for statewide.",
    )
    zip_code: str | None = Field(
        None,
        description=(
            "ZIP code(s) where the program is valid, AS EXPLICITLY STATED in the source. "
            "Single ZIP (e.g. '33601'), comma-separated list (e.g. '33601, 33602, 33603'), "
            "or range (e.g. '33601-33647'). "
            "Return null if not explicitly mentioned — do NOT infer ZIPs from city or county names."
        ),
    )
    incentive_type: str | None = Field(
        None,
        description=(
            "Must be exactly one of: Grants, Rebates, Finance Solutions, Tax Credits, Investments. "
            "Grants=upfront money not repaid. Rebates=cash back after purchase. "
            "Finance Solutions=pay over time. Tax Credits=reduce taxes owed. "
            "Investments=large-scale project funding. Null if none apply."
        ),
    )
    property_type: str | None = Field(
        None,
        description=(
            "Property types eligible as stated in the source, e.g. 'Residential', 'Commercial', "
            "'Residential & Commercial', 'Multifamily'. "
            "Do NOT use vague terms like 'Other', 'Neither', 'N/A', or 'Unknown' — use null instead."
        ),
    )
    description: str | None = Field(None, description="1-3 sentence summary of what the program offers.")
    eligibility_criteria: str | None = Field(
        None, description="Who qualifies — income limits, ownership, property type, geography, etc."
    )
    incentive_amount: str | None = Field(
        None, description="Amount as stated in source, e.g. '$10,000', '30%', 'Up to $3,200/year'."
    )
    valid_until: str | None = Field(
        None, description="Program expiry date if stated, e.g. '2032-12-31'. Null if not mentioned."
    )
    updated_at: str | None = Field(
        None,
        description=(
            "Date the program information was last updated AS SHOWN ON THE SOURCE PAGE "
            "(e.g. 'Last updated: March 2024'). Extract verbatim or convert to YYYY-MM-DD. "
            "Return null if no update date is visible — do NOT use today's date."
        ),
    )
    program_links: str | None = Field(None, description="Direct URL to apply or learn more.")


class IncentivesOutput(BaseModel):
    """Container for all incentives extracted from one page."""
    incentives: list[IncentiveExtraction] = Field(
        default_factory=list,
        description="List of all incentive programs found. Empty list if none found.",
    )


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a structured data extraction specialist for clean energy and property incentive programs \
in Tampa, FL and Hillsborough County.
Extract ALL incentive programs from the provided content and return them as structured data.

Field rules:
1. Only extract information EXPLICITLY stated in the source. Never infer or fabricate values.
2. If a field is not mentioned anywhere in the content, set it to null.
3. program_name: exact official title of the program.
4. state: "Florida" for all FL programs. Null only if genuinely unclear.
5. city: "Tampa" for Tampa city programs; "Hillsborough County" for county-wide; null for statewide/federal.
5a. zip_code: ZIP code(s) where the program applies, ONLY if explicitly listed on the page.
    Examples: "33601", "33601, 33602, 33603", "33601-33647". Null otherwise.
    NEVER infer ZIPs from city/county names — only extract if the source lists them verbatim.
6. incentive_type: must be exactly one of — Grants, Rebates, Finance Solutions, Tax Credits, Investments.
   Grants = upfront money not repaid. Rebates = cash back after purchase.
   Finance Solutions = pay over time (loans, PACE). Tax Credits = reduce taxes owed.
   Investments = large-scale equity or bond funding. Null if none apply.
7. property_type: use the exact categories the source mentions (e.g. "Residential", "Commercial").
   NEVER use vague terms like "Other", "Neither", "N/A", or "Unknown" — use null instead.
8. description: 1-3 sentences summarising what the program offers.
9. eligibility_criteria: who qualifies — income limits, ownership status, geography, equipment specs, etc.
10. incentive_amount: amount exactly as stated, e.g. "$125 per unit", "30%", "Up to $3,200/year".
    CRITICAL — capture EVERY award cap / tier mentioned for the program. A single
    program often has multiple caps for different use cases:
      • Equipment tiers, e.g. "$40 (SEER 16) / $550 (SEER 17+)"
      • Use-case tiers, e.g. "Up to $150,000 (repair); Up to $350,000 (reconstruction);
        Up to $50,000 (reimbursement, $10,000 min)"
      • Property tiers, e.g. "$1,000 single-family; $600 multifamily"
    Concatenate all tiers into ONE field separated by "; ". DO NOT pick only the
    first or smallest dollar figure. Scan the entire content for additional caps
    before finalising this field — they may appear in separate paragraphs, bullet
    lists, accordions, or FAQ sections.
11. valid_until: expiry date if stated. Null if not mentioned.
12. updated_at: date the program information was LAST UPDATED as shown on the page
    (e.g. "Last updated: March 2024"). Convert to YYYY-MM-DD if possible.
    Return null if no update date is visible on the page — do NOT use today's date.
13. program_links: direct URL to apply or learn more. Use the source URL if no specific link is given.
14. Each program is a separate entry — do not merge multiple programs into one.
15. If the page has no incentive programs, return an empty incentives list.
"""

MAX_CONTENT_CHARS = 8_000


def _build_user_prompt(source_url: str, content_type: str, raw_text: str) -> str:
    truncated = raw_text[:MAX_CONTENT_CHARS]
    if len(raw_text) > MAX_CONTENT_CHARS:
        truncated += "\n\n[Content truncated — continued beyond this point]"
    return (
        f"Extract all incentive programs from the following content.\n"
        f"Source URL: {source_url}\n"
        f"Content type: {content_type}\n"
        f"Geographic focus: Tampa, FL and Hillsborough County\n\n"
        f"---BEGIN CONTENT---\n{truncated}\n---END CONTENT---"
    )


# ── LLM factory ───────────────────────────────────────────────────────────────

def _build_llm(provider: str):
    """Return a LangChain chat model for the given provider."""
    if provider == "gemini":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY is not set in .env")
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            # gemini-1.5-flash was retired by Google; default to 2.0-flash.
            model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            temperature=0,
            google_api_key=api_key,
        )
    else:  # groq (default)
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in .env")
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=0,
            groq_api_key=api_key,
        )


# ── Parser class ───────────────────────────────────────────────────────────────

class LLMParser:
    """
    Parses raw scraped content into structured IncentiveExtraction records.

    Uses LangChain's with_structured_output() which enforces the Pydantic schema
    via tool calling under the hood — works with both Groq and Gemini.

    LangSmith tracing is automatic when LANGCHAIN_TRACING_V2=true.
    """

    def __init__(self):
        provider = os.getenv("LLM_PROVIDER", "groq").lower()
        self._provider = provider
        base_llm = _build_llm(provider)
        # with_structured_output() uses tool calling to enforce the schema
        self._llm = base_llm.with_structured_output(IncentivesOutput)
        logger.info("LLM parser initialised — provider: %s", provider)

    def parse(self, raw_content: RawContent) -> list[dict]:
        """Parse raw scraped content → list of incentive record dicts."""
        if not raw_content.success or not raw_content.raw_text.strip():
            logger.info("Skipping LLM parse for %s (no content)", raw_content.source_id)
            return []

        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=_build_user_prompt(
                    source_url=raw_content.source_url,
                    content_type=raw_content.content_type,
                    raw_text=raw_content.raw_text,
                )
            ),
        ]

        try:
            result: IncentivesOutput = self._llm.invoke(messages)
            incentives = result.incentives if result else []
            logger.info(
                "LLM extracted %d incentive(s) from %s [%s]",
                len(incentives),
                raw_content.source_id,
                self._provider,
            )
        except Exception as exc:
            err_str = str(exc).lower()
            if "rate" in err_str or "429" in err_str:
                logger.warning("Rate limit hit for %s — sleeping 30s", raw_content.source_id)
                time.sleep(30)
                try:
                    result = self._llm.invoke(messages)
                    incentives = result.incentives if result else []
                except Exception as retry_exc:
                    logger.error("Retry also failed for %s: %s", raw_content.source_id, retry_exc)
                    return []
            else:
                logger.error("LLM parse failed for %s: %s", raw_content.source_id, exc)
                return []

        # Enrich records with metadata.
        # updated_at: use what the LLM extracted from the page; do NOT overwrite
        # with today's scrape date — if the site doesn't show an update date, it
        # stays None (the LLM should already return None in that case).
        records = []
        for item in incentives:
            record = item.model_dump()
            # Only set program_links fallback; never touch updated_at here
            if not record.get("program_links") and raw_content.source_url:
                record["program_links"] = raw_content.source_url
            records.append(record)

        return records
