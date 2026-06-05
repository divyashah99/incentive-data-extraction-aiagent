"""
ZIP Code Enricher
=================
Most program pages don't list ZIP codes explicitly — they just say "Tampa
residents" or "Hillsborough County homeowners". The LLM correctly returns
zip_code=None for those (we instruct it not to fabricate). This enricher
fills the gap by mapping the program's city/county scope to the actual ZIPs
that fall within it, using the maintained crosswalk CSV (spec §5.4).

Rules:
  • zip_code already set       → leave unchanged (LLM-extracted verbatim wins)
  • city = "Tampa"             → fill Tampa city ZIPs
  • city = "Hillsborough County" → fill all Hillsborough County ZIPs
  • Anything else (statewide, federal, other cities) → leave None

This runs AFTER amount validation and BEFORE Pydantic schema validation,
so the dicts can still be mutated.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Load ZIPs from the maintained crosswalk CSV (spec §5.4) ───────────────────
# The CSV lives in geo/ alongside the pipeline. Keeping ZIPs in a data file
# (not Python code) means the data team can extend coverage without a code
# change when M2 / M3 milestones open new counties.

_CROSSWALK_PATH = Path(__file__).resolve().parent.parent / "geo" / "geo_crosswalk_tampa_hillsborough.csv"


def _load_crosswalk(path: Path) -> tuple[list[str], list[str]]:
    """Return (tampa_zips, hillsborough_county_zips) from the crosswalk CSV."""
    if not path.exists():
        logger.warning("Geo crosswalk not found at %s — ZIP enrichment disabled", path)
        return [], []

    tampa: list[str] = []
    county: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            zip_code = (row.get("zip") or "").strip()
            city = (row.get("city") or "").strip()
            if not zip_code:
                continue
            if city.lower() == "tampa":
                tampa.append(zip_code)
            county.add(zip_code)
    return tampa, sorted(county)


TAMPA_CITY_ZIPS, HILLSBOROUGH_COUNTY_ZIPS = _load_crosswalk(_CROSSWALK_PATH)


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

        if city == "tampa" and TAMPA_CITY_ZIPS:
            r["zip_code"] = ", ".join(TAMPA_CITY_ZIPS)
            enriched_count += 1
        elif city in ("hillsborough county", "hillsborough") and HILLSBOROUGH_COUNTY_ZIPS:
            r["zip_code"] = ", ".join(HILLSBOROUGH_COUNTY_ZIPS)
            enriched_count += 1
        # else: city is set but isn't Tampa/Hillsborough — leave null
        #       (we don't have lookup tables for other cities yet — spec §8 milestones)

    if enriched_count:
        logger.info("ZIP enricher populated %d record(s) from city/county scope", enriched_count)
    return records


def enrichment_summary() -> dict:
    """Return counts of ZIPs in each lookup table — for diagnostics/tests."""
    return {
        "tampa_zips": len(TAMPA_CITY_ZIPS),
        "hillsborough_zips": len(HILLSBOROUGH_COUNTY_ZIPS),
    }


def load_crosswalk_rows(path: Path | str | None = None) -> list[dict]:
    """
    Public loader for the program_geo expansion step (spec §5.3).
    Returns the raw rows from the crosswalk: [{city, zip, county}, ...].
    """
    p = Path(path) if path else _CROSSWALK_PATH
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]
