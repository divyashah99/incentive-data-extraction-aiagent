"""
ZIP Code Enricher
=================
Most program pages don't list ZIP codes explicitly — they just say "Tampa
residents" or "Hillsborough County homeowners". The LLM correctly returns
zip_code=None for those (we instruct it not to fabricate). This enricher
fills the gap by mapping the program's city/county scope to the actual ZIPs
that fall within it, using curated lookup tables.

Rules:
  • zip_code already set       → leave unchanged (LLM-extracted verbatim wins)
  • city = "Tampa"             → fill Tampa city ZIPs
  • city = "Hillsborough County" → fill all Hillsborough County ZIPs
  • Anything else (statewide, federal, other cities) → leave None

This runs AFTER amount validation and BEFORE Pydantic schema validation,
so the dicts can still be mutated.
"""
import logging

logger = logging.getLogger(__name__)

# ── ZIP lookup tables ─────────────────────────────────────────────────────────
# Source: USPS ZIP code service area data for Hillsborough County, FL (2025).
# Excludes PO-box-only and unique-recipient ZIPs (e.g. 33601, 33608, 33622)
# since those aren't useful for resident eligibility matching.

TAMPA_CITY_ZIPS: list[str] = [
    "33602", "33603", "33604", "33605", "33606", "33607", "33609",
    "33610", "33611", "33612", "33613", "33614", "33615", "33616",
    "33617", "33618", "33619", "33620", "33621", "33624", "33625",
    "33626", "33629", "33634", "33635", "33637", "33647",
]

# Hillsborough County = Tampa city + surrounding unincorporated areas
# (Brandon, Riverview, Plant City, Sun City Center, Valrico, Lutz, etc.)
_HILLSBOROUGH_NON_TAMPA = [
    # Brandon area
    "33508", "33509", "33510", "33511",
    # Riverview, Apollo Beach
    "33569", "33572", "33578", "33579",
    # Plant City
    "33563", "33564", "33565", "33566", "33567",
    # Sun City Center / Ruskin / Wimauma
    "33570", "33573", "33586", "33598",
    # Valrico, Lithia, Dover
    "33527", "33547", "33594", "33596",
    # Lutz, Odessa, Cheval (part)
    "33548", "33549", "33556", "33558", "33559",
    # Seffner, Thonotosassa, Mango, Gibsonton
    "33530", "33534", "33550", "33584", "33592",
]

HILLSBOROUGH_COUNTY_ZIPS: list[str] = sorted(set(TAMPA_CITY_ZIPS + _HILLSBOROUGH_NON_TAMPA))


# ── Enricher ──────────────────────────────────────────────────────────────────

def _normalise_city(city: str | None) -> str:
    return (city or "").strip().lower()


def enrich_zip_codes(records: list[dict]) -> list[dict]:
    """
    Fill in zip_code from city/county scope where the LLM returned null.
    Mutates and returns the input list.
    """
    enriched_count = 0

    for r in records:
        # Trust verbatim ZIPs from the LLM
        if r.get("zip_code"):
            continue

        city = _normalise_city(r.get("city"))
        if not city:
            continue   # statewide / federal / unknown — leave null

        if city == "tampa":
            r["zip_code"] = ", ".join(TAMPA_CITY_ZIPS)
            enriched_count += 1
        elif city in ("hillsborough county", "hillsborough"):
            r["zip_code"] = ", ".join(HILLSBOROUGH_COUNTY_ZIPS)
            enriched_count += 1
        # else: city is set but isn't Tampa/Hillsborough — leave null
        #       (we don't have lookup tables for other cities)

    if enriched_count:
        logger.info("ZIP enricher populated %d record(s) from city/county scope", enriched_count)
    return records


def enrichment_summary() -> dict:
    """Return counts of ZIPs in each lookup table — for diagnostics/tests."""
    return {
        "tampa_zips": len(TAMPA_CITY_ZIPS),
        "hillsborough_zips": len(HILLSBOROUGH_COUNTY_ZIPS),
    }
