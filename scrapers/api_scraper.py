"""
API Scraper — handles structured JSON API sources.
Currently supports: DSIRE Florida (direct field mapping, no LLM).
"""
import json
import logging
import re
import time
from urllib3.util.retry import Retry
import requests
from requests.adapters import HTTPAdapter

from scrapers.base_scraper import BaseScraper, RawContent

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# DSIRE field mappings
# ─────────────────────────────────────────────────────────────────────────────

DSIRE_TYPE_MAP: dict[str, str] = {
    "Rebate Program":                   "Rebates",
    "Utility Rebate Program":           "Rebates",
    "Performance-Based Incentive":      "Rebates",
    "Corporate Tax Credit":             "Tax Credits",
    "Personal Tax Credit":              "Tax Credits",
    "Corporate Tax Exemption":          "Tax Credits",
    "Personal Tax Exemption":           "Tax Credits",
    "Corporate Tax Deduction":          "Tax Credits",
    "Personal Tax Deduction":           "Tax Credits",
    "Corporate Depreciation":           "Tax Credits",
    "Property Tax Exemption":           "Tax Credits",
    "Property Tax Incentive":           "Tax Credits",
    "Property Tax Assessment":          "Tax Credits",
    "Sales Tax Exemption":              "Tax Credits",
    "Sales Tax Incentive":              "Tax Credits",
    "Value-Added Tax Exemption":        "Tax Credits",
    "Grant Program":                    "Grants",
    "Green Building Incentive":         "Grants",
    "Industry Recruitment/Support":     "Grants",
    "Generation Incentive Program":     "Grants",
    "Loan Program":                     "Finance Solutions",
    "PACE Financing":                   "Finance Solutions",
    "Leasing":                          "Finance Solutions",
    "Green Power Purchasing":           "Finance Solutions",
    "Energy Savings Performance Contract": "Finance Solutions",
    "Production Incentive":             "Finance Solutions",
    "Bond Program":                     "Investments",
    "Revolving Loan Fund":              "Investments",
}

# DSIRE returns regulations / standards / policies under typeObj.name as well.
# These are NOT incentives per spec §3.1 (programs that help property owners
# with clean energy or small-scale disaster/resilience) — they are technical
# standards or grid-interconnection rules. Route them to quarantine.csv with
# an explicit "out of scope" reason so reviewers see what was filtered.
DSIRE_NON_INCENTIVE_TYPES: set[str] = {
    "Building Energy Code",
    "Energy Standards for Public Buildings",
    "Appliance/Equipment Efficiency Standards",
    "Renewables Portfolio Standard",
    "Energy Efficiency Resource Standard",
    "Net Metering",
    "Interconnection",
    "Solar/Wind Permitting Standards",
    "Solar/Wind Access Policy",
    "Solar/Wind Contractor Licensing",
    "Equipment Certification",
    "Generation Disclosure",
}

DSIRE_SECTOR_MAP: dict[str, str] = {
    "Residential":              "Residential",
    "Low-Income Residential":   "Residential",
    "Commercial":               "Commercial",
    "Industrial":               "Commercial",
    "Multifamily Residential":  "Residential & Multifamily",
    "General Public/Consumer":  "Residential & Commercial",
    "Local Government":         "Commercial",
    "State Government":         "Commercial",
    "Federal Government":       "Commercial",
    "Utility":                  "Commercial",
    "Nonprofit":                "Commercial",
    "Institutional":            "Commercial",
    "Agricultural":             "Commercial",
    "Schools":                  "Commercial",
    "Construction":             "Residential & Commercial",
}

_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
FLORIDA_STATE_ID = 12


def _strip_html(text: str) -> str:
    if not text:
        return ""
    clean = _HTML_TAG_RE.sub(" ", text)
    for entity, repl in [("&#10;", " "), ("&#9;", " "), ("&nbsp;", " "),
                          ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&quot;", '"'), ("&#34;", '"')]:
        clean = clean.replace(entity, repl)
    return _WHITESPACE_RE.sub(" ", clean).strip()


# ─────────────────────────────────────────────────────────────────────────────
# DSIRE direct mapper
# ─────────────────────────────────────────────────────────────────────────────

def _map_dsire_property_type(param_sets: list[dict]) -> str | None:
    seen: set[str] = set()
    for ps in param_sets:
        for sector in ps.get("sectors", []):
            mapped = DSIRE_SECTOR_MAP.get(sector.get("name", ""))
            if mapped:
                seen.add(mapped)
    if not seen:
        return None
    has_res = any("Residential" in s for s in seen)
    has_com = any("Commercial" in s for s in seen)
    if has_res and has_com:
        return "Residential & Commercial"
    return "Residential" if has_res else "Commercial" if has_com else "; ".join(sorted(seen))


def _dsire_incentive_amount(details: list[dict], param_sets: list[dict]) -> str | None:
    for d in details:
        if d.get("label", "").lower() in ("incentive amount", "incentive", "amount"):
            val = _strip_html(d.get("value", ""))
            if val:
                return val[:500]
    amounts = []
    for ps in param_sets:
        for p in ps.get("parameters", []):
            amt, units, qual = p.get("amount", ""), p.get("units", ""), p.get("qualifier", "")
            if amt and float(amt) != 0:
                amounts.append(f"{qual} {amt} {units}".strip())
    return "; ".join(amounts[:5]) if amounts else None


def _dsire_eligibility(details: list[dict], param_sets: list[dict]) -> str | None:
    parts = []
    want = {"eligible technologies", "eligible sectors", "equipment requirements",
            "eligible system size", "eligible recipient", "installation requirements",
            "eligibility requirements"}
    for d in details:
        if d.get("label", "").lower() in want:
            val = _strip_html(d.get("value", ""))
            if val:
                parts.append(f"{d['label']}: {val[:200]}")
    techs = {t.get("name") for ps in param_sets for t in ps.get("technologies", []) if t.get("name")}
    if techs:
        parts.append(f"Eligible technologies: {', '.join(sorted(techs))}")
    return "; ".join(parts)[:800] if parts else None


def _parse_dsire_program(prog: dict) -> dict:
    state_name = prog.get("stateObj", {}).get("name", "")
    state      = "Florida" if state_name in ("Florida", "Federal") else state_name
    param_sets = prog.get("parameterSets", [])
    details    = prog.get("details", [])

    raw_type_name    = prog.get("typeObj", {}).get("name", "") or ""
    incentive_type   = DSIRE_TYPE_MAP.get(raw_type_name)
    property_type    = _map_dsire_property_type(param_sets)
    description      = _strip_html(prog.get("summary", ""))[:600] or None
    eligibility      = _dsire_eligibility(details, param_sets)
    incentive_amount = _dsire_incentive_amount(details, param_sets)
    valid_until      = prog.get("endDateDisplay") or prog.get("endDate") or None
    if valid_until == "":
        valid_until = None

    reasons: list[str] = []
    if raw_type_name in DSIRE_NON_INCENTIVE_TYPES:
        reasons.append(f"out of scope: {raw_type_name}")
    elif raw_type_name and incentive_type is None:
        reasons.append(f"unmapped DSIRE type: {raw_type_name}")

    return {
        "program_name":         prog.get("name"),
        "state":                state,
        "city":                 None,
        "zip_code":             None,   # DSIRE programs are statewide; no ZIP scoping
        "incentive_type":       incentive_type,
        "service_category":     None,   # spec §4.4 — populated later when source names categories
        "property_type":        property_type,
        "description":          description,
        "eligibility_criteria": eligibility,
        "incentive_amount":     incentive_amount,
        "valid_until":          valid_until,
        "updated_at":           prog.get("lastUpdated") or prog.get("updatedTs") or None,
        "program_links":        prog.get("websiteUrl") or
                                f"https://programs.dsireusa.org/system/program/detail/{prog.get('id')}",
        "_quarantine_reasons":  reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ApiScraper class
# ─────────────────────────────────────────────────────────────────────────────

class ApiScraper(BaseScraper):

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
        session.headers.update({"Accept": "application/json",
                                 "Referer": "https://programs.dsireusa.org/"})
        return session

    def fetch(self, source: dict) -> RawContent:
        source_id = source.get("id", "unknown")
        self.rate_limit_seconds = source.get("rate_limit_seconds", self.rate_limit_seconds)

        try:
            if source_id == "dsire_florida":
                return self._fetch_dsire(source)
            else:
                data = self._get_generic(source)
                return RawContent(
                    source_id=source_id,
                    source_url=source.get("base_url", ""),
                    content_type="json",
                    raw_text=json.dumps(data, indent=2),
                )
        except Exception as exc:
            logger.warning("API fetch failed for %s: %s", source_id, exc)
            return RawContent(
                source_id=source_id,
                source_url=source.get("base_url", ""),
                content_type="json",
                success=False,
                error_message=str(exc),
            )

    # ── DSIRE ─────────────────────────────────────────────────────────────────

    def _fetch_dsire(self, source: dict) -> RawContent:
        base_url  = source["base_url"]
        state_id  = source.get("api_params", {}).get("state", FLORIDA_STATE_ID)
        page_size = source.get("api_params", {}).get("length", 50)

        all_programs: list[dict] = []
        draw, start = 1, 0

        logger.info("Fetching DSIRE Florida programs (state_id=%s)…", state_id)
        while True:
            self._rate_limit()
            params = {"draw": draw, "start": start, "state": state_id, "length": page_size}
            resp = self._session.get(base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            page = resp.json()

            records = page.get("data", [])
            if not records:
                break
            all_programs.extend(records)
            logger.info("  DSIRE page draw=%d: +%d (total %d)", draw, len(records), len(all_programs))

            total = page.get("recordsTotal", page.get("recordsFiltered", 0))
            if total and start + page_size >= total:
                break
            if len(records) < page_size:
                break
            draw += 1
            start += page_size
            time.sleep(self.rate_limit_seconds)

        logger.info("DSIRE complete — %d programs", len(all_programs))
        parsed = [_parse_dsire_program(p) for p in all_programs]
        return RawContent(
            source_id=source["id"],
            source_url=base_url,
            content_type="structured",
            parsed_records=parsed,
        )

    # ── Generic JSON API ──────────────────────────────────────────────────────

    def _get_generic(self, source: dict) -> list | dict:
        self._rate_limit()
        resp = self._session.get(
            source["base_url"],
            params=source.get("api_params", {}),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()
