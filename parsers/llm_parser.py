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
    program_name: str | None = Field(
        None,
        description=(
            "Official name of the incentive program. For programs that have "
            "multiple distinct award pathways (different application tracks, "
            "each with its own eligibility and cap), use the format "
            "'<Parent Program> — <Pathway Name>' "
            "(e.g. 'HRRP — Storm Damage Repair', 'HRRP — Reconstruction & Replacement') "
            "and emit one record per pathway."
        ),
    )
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
    description: str | None = Field(
        None,
        description=(
            "Full summary of what the program offers — what kind of work it funds, "
            "how the funding is delivered, and any program-purpose context. "
            "Include all relevant details stated on the page; do not truncate."
        ),
    )
    eligibility_criteria: str | None = Field(
        None,
        description=(
            "All eligibility requirements stated on the page — income limits, "
            "ownership status, property type, geography, equipment specs, occupancy, "
            "primary-residence rules, etc. Include every criterion you find; do not summarise away."
        ),
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
You extract clean energy and property incentive programs for Tampa, FL and \
Hillsborough County from the provided content.

Core rule: only use information EXPLICITLY stated in the source. Never infer,
fabricate, or backfill from general knowledge — set unknowns to null.

Field rules:
1. program_name: exact official title. If a program has multiple distinct
   award pathways (separate application tracks, each with its own cap and
   eligibility), emit ONE record per pathway named "<Parent> — <Pathway>"
   (e.g. "HRRP — Storm Damage Repair").
2. state: "Florida" for FL programs, null if unclear.
3. city: "Tampa" for city-only, "Hillsborough County" for county-wide,
   null for statewide/federal.
4. zip_code: only if listed verbatim on the page. NEVER derive from city
   or county names.
5. incentive_type: exactly one of:
     • Grants — upfront money not repaid
     • Rebates — cash back after purchase
     • Finance Solutions — pay over time (loans, PACE)
     • Tax Credits — reduce taxes owed
     • Investments — large-scale equity or bond funding
   Null if none apply.
6. property_type: source's wording (e.g. "Residential", "Commercial").
   Never use vague terms like "Other", "N/A", "Unknown" — use null instead.
7. description: full summary of what THIS pathway offers — scope, what it
   funds, and any exclusions ("not eligible: X") stated on the page. For
   split records, describe the pathway specifically, not the parent program
   in general.
8. eligibility_criteria: ALL eligibility statements — both inclusions and
   exclusions, damage/income thresholds, minimums, compliance rules. Capture
   every condition stated for THIS pathway. Do not summarise away.
   Both 7 and 8 must be plain narrative — NEVER include URLs or phrases
   like "visit", "apply at", "click here". The dashboard has its own apply
   button; state facts, not navigation.
9. incentive_amount: amount with ALL qualifiers — caps, percentages,
   minimums, alternate formulas. Examples:
     • "Up to $150,000 or 50% of pre-storm value"
     • "Up to $50,000 ($10,000 minimum)"
   Split vs. concatenate:
     • Separate pathways → split into multiple records (per rule 1), each
       with only its own amount.
     • Tiers WITHIN one award (equipment specs, property-type variants) →
       keep one record, concatenate with "; "
       (e.g. "$40 (SEER 16); $550 (SEER 17+)").
10. valid_until: expiry date if stated, else null.
11. updated_at: page's last-updated date if shown, converted to YYYY-MM-DD.
    NEVER use today's date — null if not shown.
12. program_links: direct apply/info URL, or the source URL as fallback.

Return an empty list if the page has no incentive programs.
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
