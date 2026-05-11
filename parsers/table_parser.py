"""
Table Parser — extracts incentive records directly from HTML tables using BeautifulSoup.
No LLM involved. Used when extraction_method = "table" in sources.yaml.

Strategy:
  1. Find all <table> elements on the page
  2. For each table, extract headers + rows as dicts
  3. Use heuristic column-name matching to map to our 12-column schema
  4. If no usable table is found, return empty list (pipeline falls back to LLM)
"""
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Column-name heuristics ────────────────────────────────────────────────────
# Each schema field maps to a list of substrings to look for in table headers.
# First match wins, case-insensitive.

HEADER_MAP: dict[str, list[str]] = {
    "program_name":         ["program", "name", "product", "measure", "item", "description", "service"],
    "incentive_type":       ["type", "category", "incentive type", "program type"],
    "property_type":        ["property", "sector", "building", "customer", "eligible property"],
    "incentive_amount":     ["amount", "rebate", "incentive", "credit", "discount", "value", "savings", "$"],
    "eligibility_criteria": ["eligib", "requirement", "qualify", "who", "criteria", "condition"],
    "valid_until":          ["expir", "deadline", "end date", "valid", "through", "until"],
    "description":          ["detail", "summary", "about", "note", "comment"],
}

# Incentive type keywords for heuristic classification when no type column exists
_GRANT_KW     = re.compile(r"\bgrant\b", re.I)
_REBATE_KW    = re.compile(r"\brebate\b|\bcash back\b", re.I)
_TAXCRED_KW   = re.compile(r"\btax credit\b|\btax deduct\b|\btax exempt\b", re.I)
_FINANCE_KW   = re.compile(r"\bloan\b|\bfinancin\b|\bpace\b|\binstalment\b", re.I)
_INVEST_KW    = re.compile(r"\binvestment\b|\bbond\b|\bfund\b", re.I)

VALID_INCENTIVE_TYPES = {"Grants", "Rebates", "Finance Solutions", "Tax Credits", "Investments"}


def _infer_incentive_type(text: str) -> str | None:
    if _REBATE_KW.search(text):    return "Rebates"
    if _GRANT_KW.search(text):     return "Grants"
    if _TAXCRED_KW.search(text):   return "Tax Credits"
    if _FINANCE_KW.search(text):   return "Finance Solutions"
    if _INVEST_KW.search(text):    return "Investments"
    return None


def _match_header(header_text: str) -> str | None:
    """Return the schema field name that best matches a table header string."""
    h = header_text.lower().strip()
    for field, keywords in HEADER_MAP.items():
        if any(kw in h for kw in keywords):
            return field
    return None


def _extract_tables(html: str) -> list[list[dict]]:
    """
    Parse all <table> elements from raw HTML.
    Returns a list of tables; each table is a list of row-dicts
    keyed by the original header text.
    """
    soup = BeautifulSoup(html, "lxml")
    tables: list[list[dict]] = []

    for table in soup.find_all("table"):
        headers: list[str] = []
        rows: list[dict] = []

        # Try <thead> first, then first <tr> with <th> tags
        thead = table.find("thead")
        if thead:
            headers = [th.get_text(strip=True) for th in thead.find_all(["th", "td"])]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                ths = first_row.find_all("th")
                if ths:
                    headers = [th.get_text(strip=True) for th in ths]

        if not headers:
            continue  # no usable header → skip this table

        # Extract data rows
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells or len(cells) < 2:
                continue
            row = {}
            for i, cell in enumerate(cells):
                key = headers[i] if i < len(headers) else f"col_{i}"
                # Preserve links inside cells
                link = cell.find("a")
                row[key] = cell.get_text(" ", strip=True)
                if link and link.get("href"):
                    row[f"__link_{key}"] = link["href"]
            rows.append(row)

        if rows:
            tables.append(rows)

    return tables


def _row_to_record(row: dict, source_url: str, source_name: str) -> dict:
    """
    Map one table row dict to our schema dict using header heuristics.
    Returns a partial record — missing fields stay None.
    """
    record: dict = {
        "program_name":         None,
        "state":                "Florida",
        "city":                 None,
        "incentive_type":       None,
        "property_type":        None,
        "description":          None,
        "eligibility_criteria": None,
        "incentive_amount":     None,
        "valid_until":          None,
        "updated_at":           None,   # table pages rarely show an update date
        "program_links":        source_url,
        "confidence_score":     0.7,   # table data is reliable but mapping is heuristic
    }

    # Map each column using header heuristics
    for col_key, cell_value in row.items():
        if col_key.startswith("__link_"):
            continue
        schema_field = _match_header(col_key)
        if schema_field and not record.get(schema_field):
            record[schema_field] = cell_value.strip() if cell_value else None

    # Capture any link found in the row as program_links
    link_values = [v for k, v in row.items() if k.startswith("__link_")]
    if link_values:
        href = link_values[0]
        if href.startswith("http"):
            record["program_links"] = href
        elif href.startswith("/"):
            from urllib.parse import urlparse
            base = urlparse(source_url)
            record["program_links"] = f"{base.scheme}://{base.netloc}{href}"

    # If no program_name was found, skip this row
    if not record["program_name"]:
        return {}

    # Infer incentive_type from the row text if not captured
    if not record["incentive_type"]:
        full_text = " ".join(str(v) for v in row.values())
        record["incentive_type"] = (
            _infer_incentive_type(full_text)
            or _infer_incentive_type(source_name)
        )

    # Confidence: penalise heavily missing required fields
    missing = sum(1 for f in ("incentive_amount", "description", "incentive_type")
                  if not record.get(f))
    record["confidence_score"] = max(0.4, 0.7 - missing * 0.1)

    return record


def parse_tables(raw_html: str, source: dict) -> list[dict]:
    """
    Main entry point. Parse HTML tables from raw_html and return
    a list of schema-compatible dicts. Returns [] if no usable tables found
    (pipeline will fall back to LLM in that case).
    """
    source_url  = source.get("url", source.get("base_url", ""))
    source_name = source.get("name", "")
    source_id   = source.get("id", "unknown")

    all_tables = _extract_tables(raw_html)

    if not all_tables:
        logger.info("table_parser [%s]: no tables found — will fall back to LLM", source_id)
        return []

    records: list[dict] = []
    for table_idx, table_rows in enumerate(all_tables):
        for row in table_rows:
            rec = _row_to_record(row, source_url, source_name)
            if rec:
                records.append(rec)

    if records:
        logger.info(
            "table_parser [%s]: extracted %d record(s) from %d table(s) — no LLM used",
            source_id, len(records), len(all_tables),
        )
    else:
        logger.info(
            "table_parser [%s]: tables found but no mappable rows — will fall back to LLM",
            source_id,
        )

    return records
