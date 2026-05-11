# Incentive Data Extraction AI Agent

An automated pipeline that discovers, scrapes, parses, validates, and exports clean energy and property incentive programs for **Tampa, FL and Hillsborough County**. Built for [Dreamline AI](https://dreamlineai.org).

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Pipeline Walkthrough](#pipeline-walkthrough)
   - [Step 1 — Discover & Load Sources](#step-1--discover--load-sources)
   - [Step 2 — Scrape](#step-2--scrape)
   - [Step 3 — Extract / Parse](#step-3--extract--parse)
   - [Step 4 — Validate](#step-4--validate)
   - [Step 5 — Output](#step-5--output)
5. [Data Sources](#data-sources)
6. [Output Schema](#output-schema)
7. [LLM Provider Configuration](#llm-provider-configuration)
8. [LangSmith Monitoring](#langsmith-monitoring)
9. [Setup & Installation](#setup--installation)
10. [Running the Pipeline](#running-the-pipeline)
11. [Configuration Reference](#configuration-reference)
12. [Known Limitations & Notes](#known-limitations--notes)

---

## Overview

The pipeline aggregates incentive programs from ~25 sources spanning:

- **Federal** — IRS tax credits (25C, 25D), FEMA grants, ENERGY STAR
- **State** — DSIRE Florida database, Florida Housing Finance Corp, FDACS Energy
- **County** — Hillsborough County housing grants, OOR rehab loans, DPA
- **City** — Tampa Housing & Community Development, My Safe Florida Home
- **Utility** — Tampa Electric (TECO), Duke Energy Florida
- **Finance** — PACE financing (Ygrene)

Each source has a **priority tier** (P0 / P1 / P2), a **scraper type** (static HTML, JS-rendered, or API), and an **extraction method** (direct mapping, table parsing, or LLM). The pipeline is fully fault-tolerant: every error is logged and the run continues.

---

## Architecture

```
  config/sources.yaml
         |
         v
  ┌─────────────────┐
  │   Discoverer    │  Loads source list, routes to correct scraper
  └────────┬────────┘
           |
           v
  ┌─────────────────────────────────────────────────────┐
  │                    SCRAPE LAYER                      │
  │                                                      │
  │  StaticScraper      JsScraper          ApiScraper   │
  │  (requests+BS4)     (Playwright)       (JSON/HTTP)  │
  │  static_html        js_rendered        api          │
  └────────────────────────┬────────────────────────────┘
                           |
                     RawContent object
                    (raw_text, raw_html,
                     parsed_records)
                           |
           ┌───────────────┼───────────────┐
           |               |               |
           v               v               v
      direct mapping   parse_tables()   LLMParser
      (no LLM)         (BS4 tables)     (LangChain +
      e.g. DSIRE API   no LLM           Groq / Gemini)
           |               |               |
           └───────────────┴───────────────┘
                           |
                    list[dict] records
                           |
                           v
  ┌─────────────────────────────────────────────────────┐
  │                   VALIDATE LAYER                     │
  │                                                      │
  │  AmountValidator          SchemaValidator            │
  │  (normalise + sanity)     (Pydantic v2 model)       │
  │  rule-based               sets review_needed         │
  └────────────────────────┬────────────────────────────┘
                           |
                    list[IncentiveRecord]
                           |
                           v
  ┌─────────────────────────────────────────────────────┐
  │                   OUTPUT LAYER                       │
  │                                                      │
  │  OutputWriter                                        │
  │  deduplicate() -> write CSV + write_xlsx()          │
  └─────────────────────────────────────────────────────┘
         |                          |
         v                          v
  output/extracted_         output/extracted_
  tampa_incentives.csv      tampa_incentives.xlsx
                            (All Records + 1 sheet
                             per source)
```

---

## Project Structure

```
incentive-data-extraction-aiagent/
│
├── config/
│   └── sources.yaml          # All scraping targets — URL, type, priority, extraction method
│
├── scrapers/
│   ├── base_scraper.py       # RawContent dataclass + rate-limiting BaseScraper ABC
│   ├── static_scraper.py     # requests + BeautifulSoup (plain HTTP pages)
│   ├── js_scraper.py         # Playwright headless Chromium (JS-rendered SPAs)
│   ├── api_scraper.py        # DSIRE paginated API + generic JSON fetcher
│   └── pdf_scraper.py        # pdfplumber (PDF sources, if any)
│
├── parsers/
│   ├── llm_parser.py         # LangChain structured output via Groq or Gemini
│   └── table_parser.py       # BeautifulSoup HTML table extractor (no LLM)
│
├── validators/
│   ├── schema_validator.py   # Pydantic v2 model + review_needed logic
│   └── amount_validator.py   # Rule-based amount normalisation & sanity checks
│
├── agents/
│   ├── discoverer.py         # Reads sources.yaml, scraper factory
│   └── output_writer.py      # CSV + multi-sheet Excel writer + deduplication
│
├── pipeline.py               # Main entry point (CLI)
├── requirements.txt
├── .env                      # API keys and settings (never commit)
├── .env.example              # Template
├── .gitignore
├── logs/                     # Created at runtime — pipeline.log
└── output/                   # Created at runtime — CSV and XLSX
```

---

## Pipeline Walkthrough

### Step 1 — Discover & Load Sources

`agents/discoverer.py` reads `config/sources.yaml` and:

- Filters by `enabled: true`
- Applies optional `--priority` or `--source` CLI filters
- Returns the source list sorted by priority (P0 first)
- Acts as a **scraper factory**: returns the correct scraper class based on `type`:
  - `static_html` → `StaticScraper`
  - `js_rendered`  → `JsScraper`
  - `api`          → `ApiScraper`

---

### Step 2 — Scrape

All scrapers extend `BaseScraper` and always return a `RawContent` object — they **never raise exceptions**. Failures set `success=False` with an `error_message`.

**`RawContent` fields:**

| Field | Description |
|---|---|
| `source_id` | Unique ID from sources.yaml (e.g. `teco_ductwork`) |
| `source_url` | The URL that was fetched |
| `content_type` | `"html"`, `"json"`, or `"structured"` |
| `raw_text` | Cleaned plain text (boilerplate stripped) |
| `raw_html` | Original HTML (needed by table parser) |
| `parsed_records` | Pre-mapped records (only for API sources) |
| `success` | `True` / `False` |
| `error_message` | Set if `success=False` |

#### StaticScraper (`static_html`)

Uses `requests.Session` with `urllib3.Retry` (3 retries, backoff on 429/5xx). After fetching, BeautifulSoup strips `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, `<aside>`, and `<noscript>` before extracting plain text.

**SSL fallback:** Sites like `floridahousing.org` serve an incomplete certificate chain. The scraper catches `SSLError` and automatically retries with `verify=False` so public marketing content is still accessible.

#### JsScraper (`js_rendered`)

Uses Playwright (headless Chromium) for sites that require JavaScript to render content — SPAs, React apps, and sites with bot-detection WAFs (e.g. Duke Energy returns HTTP 403 to plain `requests`).

Key behaviours:
- Default `wait_until="domcontentloaded"` rather than `"networkidle"` — some SPAs (Duke Energy) continuously poll analytics endpoints and never reach network idle
- If `domcontentloaded` itself times out, falls back to `"commit"` (fires as soon as the first byte arrives)
- Sources can opt in to stricter waits via `js_wait_until: networkidle` in sources.yaml
- A 1500 ms settle delay runs after page load for SPAs that hydrate after the initial DOM event
- Sources can add extra settle time via `js_wait_seconds: 3` for slow pages

#### ApiScraper (`api`)

Currently implements the **DSIRE DataTables API** (paginated JSON). It:
- Pages through all Florida programs (`state_id=12`) with 50 records per page
- Directly maps DSIRE fields to the output schema — no LLM involved
- Converts DSIRE `typeObj.name` values to the 5 incentive types via `DSIRE_TYPE_MAP`
- Converts `sectors` to `property_type` via `DSIRE_SECTOR_MAP`
- Returns results in `RawContent.parsed_records` (bypasses both table and LLM parsers)

Rate limiting is enforced on every scraper via `BaseScraper._rate_limit()` using `time.monotonic()`. The configured delay in sources.yaml (default 1.0 s, 2.0 s for federal sites) is respected between requests.

---

### Step 3 — Extract / Parse

The pipeline routes each `RawContent` to the correct parser based on the source's `extraction_method`:

```
extraction_method = direct  → use parsed_records as-is (DSIRE)
extraction_method = table   → parse_tables() from raw_html
                              if no tables found, fall back to LLM
extraction_method = llm     → LLMParser (Groq or Gemini)
```

#### LLM Parser (`parsers/llm_parser.py`)

This is the core extraction component. It uses **LangChain's `with_structured_output()`** which enforces the Pydantic schema via tool calling under the hood — the LLM cannot return free-form JSON, it must produce a valid `IncentivesOutput` object.

**Model:** Configurable via `.env`:
- **Groq** (default) — `llama-3.3-70b-versatile`, very fast, 500K tokens/day free tier
- **Gemini** (fallback) — `gemini-2.0-flash`, ~1M tokens/day free tier

**Prompt strategy:**
- A detailed system prompt with 15 field-level rules (no hallucination, exact type values, city scoping, etc.)
- Content is truncated to 8,000 characters to stay within context limits
- The LLM is instructed to return null for any field not explicitly present in the source — it must never infer or fabricate data

**Rate limit handling:** If a 429 (rate limit exceeded) error occurs, the parser sleeps 30 seconds and retries once before giving up and returning an empty list.

**Post-extraction enrichment:** Only `program_links` gets a fallback (source URL) if the LLM didn't find one. `updated_at` is intentionally never overwritten — if the source page doesn't show an update date, it stays `null`.

#### Table Parser (`parsers/table_parser.py`)

Used for pages with clean HTML tables (e.g. structured government data). BeautifulSoup extracts table rows and maps them to field names. Falls through to the LLM automatically if no suitable tables are found.

---

### Step 4 — Validate

Validation has two independent stages, both of which run on every record:

#### Amount Validator (`validators/amount_validator.py`)

Pure rule-based, no LLM. Runs **before** Pydantic for speed:

1. **Normalise** — standardises formatting (`$300.00` → `$300`, `30 %` → `30%`, `$1.2M` → `$1,200,000`)
2. **Extract** — parses the numeric value (dollar or percentage), detects `up to`/`max` qualifiers, detects annual (`/year`) rates
3. **Sanity-check** — flags suspicious amounts:
   - Amount is `$0` or missing
   - Dollar amount > $10,000,000 (likely a parsing error)
   - Dollar amount < $1 **unless** it's a per-unit rate (e.g. `$0.15/kWh`, `$125/unit`)
   - Percentage > 100%
   - No numeric value found at all

Flagged records have `review_needed` set to `"Yes"` in the raw dict before Pydantic validation runs.

#### Schema Validator (`validators/schema_validator.py`)

Pydantic v2 `BaseModel` with a `@model_validator` that sets `review_needed = "Yes"` if any of the following are true:

| Condition | Flag reason |
|---|---|
| `program_name` is null or empty | `missing program_name` |
| `incentive_type` not in the 5 valid types | `missing or invalid incentive_type` |
| `description` is null or < 10 characters | `missing or too-short description` |
| `incentive_amount` is null | `missing incentive_amount` |
| `state` is set but not Florida/FL/Federal | `unexpected state: <value>` |

**Records are never dropped.** Every record reaches the output, whether clean (`review_needed=No`) or flagged (`review_needed=Yes`). Optional fields (`city`, `property_type`, `eligibility_criteria`, `valid_until`, `updated_at`) do not trigger flags on their own — they are legitimately absent for many programs.

The 5 valid incentive types (case-insensitive normalisation applied):

| Type | Meaning |
|---|---|
| `Grants` | Upfront money not repaid |
| `Rebates` | Cash back after a qualifying purchase |
| `Finance Solutions` | Pay over time (loans, PACE financing) |
| `Tax Credits` | Reduce taxes owed |
| `Investments` | Large-scale equity or bond funding |

---

### Step 5 — Output

#### Deduplication

Before writing, `OutputWriter.deduplicate()` removes duplicate records keyed on `(program_name, incentive_type)` (case-insensitive). When duplicates exist — for example, DSIRE and a utility site both list the same program — the record with `review_needed=No` is kept.

#### CSV Output

Written with UTF-8 BOM encoding (`utf-8-sig`) so Excel opens it correctly without an import wizard. Column order matches the spec exactly:

```
program_name, state, city, incentive_type, property_type, description,
eligibility_criteria, incentive_amount, valid_until, updated_at,
review_needed, program_links
```

#### Excel Output (multi-sheet)

The XLSX workbook contains:
- **Sheet 1 — "All Records"**: all deduplicated records combined
- **Sheets 2–N**: one sheet per source ID (sorted alphabetically), showing only that source's records before deduplication

Formatting features:
- Dark blue bold header row
- Soft yellow highlight for rows where `review_needed = Yes`
- Frozen top row (header stays visible while scrolling)
- Pre-set column widths (e.g. `description` = 60, `program_name` = 40)
- Word-wrapped text cells

---

## Data Sources

### Priority P0 — Highest (run first, most reliable)

| Source ID | Name | Type | Extraction |
|---|---|---|---|
| `dsire_florida` | DSIRE Florida Database | API (paginated JSON) | direct |
| `teco_ductwork` | TECO Rebate — Ductwork | static_html | llm |
| `teco_ceiling_insulation` | TECO Rebate — Ceiling Insulation | static_html | llm |
| `teco_heating_cooling` | TECO Rebate — Heating & Cooling | static_html | llm |
| `teco_new_construction` | TECO Rebate — Energy Star New Construction | static_html | llm |
| `teco_smart_thermostat` | TECO Rebate — Smart Thermostat | static_html | llm |
| `teco_weatherization` | TECO Rebate — Weatherization | static_html | llm |
| `irs_home_energy_hub` | IRS Home Energy Tax Credits Hub | static_html | llm |
| `irs_energy_credits` | IRS Energy Efficient Home Improvement (25C) | static_html | llm |
| `irs_residential_clean_energy` | IRS Residential Clean Energy Credit (25D) | static_html | llm |

### Priority P1 — High

| Source ID | Name | Type | Extraction |
|---|---|---|---|
| `my_safe_florida` | My Safe Florida Home Program | static_html | llm |
| `irs_obbba_legislation` | IRS One Big Beautiful Bill Act | static_html | llm |
| `energystar_tax_credits` | ENERGY STAR Federal Tax Credits | static_html | llm |
| `florida_housing_homebuyer` | Florida Housing — Homebuyer Programs | static_html | llm |
| `florida_housing_ship` | Florida Housing — SHIP Program | static_html | llm |
| `florida_housing_hometown_heroes` | Florida Hometown Heroes Housing | static_html | llm |
| `hillsborough_housing` | Hillsborough County Housing (SHIP/CDBG/HOME) | static_html | llm |
| `hillsborough_oor` | Hillsborough County OOR Rehab Program | static_html | llm |
| `hillsborough_dpa` | Hillsborough County Down Payment Assistance | static_html | llm |
| `duke_energy_florida` | Duke Energy Florida — Home Energy Rebates | js_rendered | llm |
| `duke_energy_clean_energy` | Duke Energy Florida — Clean Energy Connection | js_rendered | llm |

### Priority P2 — Standard

| Source ID | Name | Type | Extraction |
|---|---|---|---|
| `tampa_community_dev` | City of Tampa — Housing Rehab (HRRP) | static_html | llm |
| `fema_hazard_mitigation` | FEMA Hazard Mitigation Grant Program | static_html | llm |
| `ygrene_pace` | Ygrene PACE Financing (Florida) | static_html | llm |
| `florida_dept_energy` | FDACS Energy Division | js_rendered | llm |
| `renewpace` | RenewPACE Florida | — | **disabled** |

> **Note:** `renewpace` is disabled — CloudFront WAF blocks all programmatic clients including Playwright (datacenter IP detection). PACE financing is already covered by `ygrene_pace`.

---

## Output Schema

| Column | Description | Example |
|---|---|---|
| `program_name` | Official program name | `TECO Ductwork Rebate` |
| `state` | State name | `Florida` |
| `city` | `Tampa`, `Hillsborough County`, or null for statewide/federal | `Tampa` |
| `incentive_type` | One of the 5 valid types | `Rebates` |
| `property_type` | As stated in source | `Residential` |
| `description` | 1–3 sentence summary | `Cash rebate for...` |
| `eligibility_criteria` | Who qualifies | `Homeowners in TECO service area...` |
| `incentive_amount` | Amount as stated in source | `$125 per unit` |
| `valid_until` | Expiry date if stated (YYYY-MM-DD) | `2032-12-31` |
| `updated_at` | Last updated date from the source page | `2024-03-01` |
| `review_needed` | `Yes` if any validation rule fired | `No` |
| `program_links` | Direct URL to apply or learn more | `https://...` |

`updated_at` is extracted verbatim from the source page (e.g. "Last updated: March 2024"). It is **never** set to today's scrape date — if no update date is visible on the page, the field is `null`.

---

## LLM Provider Configuration

Two free-tier providers are supported. Switch between them in `.env`:

```env
LLM_PROVIDER=groq     # default — Groq (llama-3.3-70b-versatile)
# LLM_PROVIDER=gemini  # fallback — Google Gemini 2.0 Flash
```

### Groq (default)

- **Free tier:** 14,400 requests/day, 500K tokens/day
- **Speed:** Very fast (~1–2 s per call)
- **Model:** `llama-3.3-70b-versatile` (best quality); `llama3-8b-8192` (faster, lighter)
- **Get key:** https://console.groq.com

```env
GROQ_API_KEY=your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

> **Token cap:** The 500K tokens/day cap is the binding limit (not requests). A full pipeline run across all ~25 sources uses approximately 400K–600K tokens. If the cap is hit mid-run, the parser sleeps 30 s and retries once; if that also fails, the source is skipped and logged.

### Google Gemini (fallback)

- **Free tier:** 15 requests/minute, ~1M tokens/day
- **Model:** `gemini-2.0-flash` (stable, free-tier)
- **Get key:** https://aistudio.google.com/apikey

```env
GOOGLE_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.0-flash
```

> **Important:** `gemini-1.5-flash` was retired by Google in 2025. Always use `gemini-2.0-flash` or `gemini-2.5-flash`.

---

## LangSmith Monitoring

[LangSmith](https://smith.langchain.com) provides automatic tracing of every LLM call — inputs, outputs, latency, token counts — with zero code changes. Highly recommended for debugging extraction quality.

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_langsmith_key
LANGCHAIN_PROJECT=dreamline-incentive-extraction
```

Sign up free at https://smith.langchain.com. Once set, every `LLMParser.parse()` call is traced automatically.

---

## Setup & Installation

### Prerequisites

- Python 3.11+
- A Groq API key (free) **or** a Google AI Studio API key (free)

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd incentive-data-extraction-aiagent
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browser

Playwright requires a one-time browser download:

```bash
playwright install chromium
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```env
LLM_PROVIDER=groq
GROQ_API_KEY=your_groq_key_here
```

---

## Running the Pipeline

### Full run (all enabled sources)

```bash
python pipeline.py
```

### P0 sources only (fastest, most reliable — good for testing)

```bash
python pipeline.py --priority P0
```

### Single source

```bash
python pipeline.py --source teco_ductwork
python pipeline.py --source dsire_florida
```

### Dry run (fetch + parse, no output files written)

```bash
python pipeline.py --dry-run
python pipeline.py --priority P0 --dry-run
```

### Custom output path

```bash
python pipeline.py --output data/tampa_incentives_2026.csv
```

### Output

After a successful run:

```
output/
├── extracted_tampa_incentives.csv   # All records, UTF-8 BOM, Excel-ready
└── extracted_tampa_incentives.xlsx  # Multi-sheet workbook
    ├── All Records                  # Combined deduplicated
    ├── dsire_florida                # DSIRE records only
    ├── teco_ductwork                # TECO ductwork only
    └── ...                         # One sheet per source
```

Logs are written to `logs/pipeline.log` and also printed to stdout.

### Pipeline summary (printed after every run)

```
=======================================================
  PIPELINE SUMMARY
=======================================================
  Sources processed : 24/25
  Sources failed    : 1
  Records extracted : 87
  Records validated : 87
  Need review       : 12
  Output (CSV)      : output/extracted_tampa_incentives.csv
  Output (XLSX)     : output/extracted_tampa_incentives.xlsx
=======================================================
```

---

## Configuration Reference

### `config/sources.yaml` fields

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique snake_case identifier |
| `name` | yes | Human-readable display name |
| `priority` | yes | `P0`, `P1`, or `P2` |
| `type` | yes | `static_html`, `js_rendered`, or `api` |
| `extraction_method` | yes | `direct`, `table`, or `llm` |
| `enabled` | yes | `true` / `false` |
| `url` | for HTML/JS | Target page URL |
| `base_url` | for API | API base endpoint |
| `api_params` | for API | Query params dict |
| `rate_limit_seconds` | no | Min seconds between requests (default: 1.0) |
| `js_wait_selector` | JS only | CSS selector to wait for before extracting |
| `js_wait_seconds` | JS only | Extra settle time in seconds (default: 0) |
| `js_wait_until` | JS only | Playwright wait event override (default: `domcontentloaded`) |
| `notes` | no | Developer notes (not used at runtime) |

### `.env` variables

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | `groq` or `gemini` | `groq` |
| `GROQ_API_KEY` | Groq API key | — |
| `GROQ_MODEL` | Groq model name | `llama-3.3-70b-versatile` |
| `GOOGLE_API_KEY` | Google AI Studio API key | — |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.0-flash` |
| `LANGCHAIN_TRACING_V2` | Enable LangSmith tracing | `false` |
| `LANGCHAIN_API_KEY` | LangSmith API key | — |
| `LANGCHAIN_PROJECT` | LangSmith project name | — |
| `PLAYWRIGHT_HEADLESS` | `1` = headless, `0` = show browser | `1` |
| `REQUEST_DELAY_SECONDS` | Global scraper delay | `1.0` |
| `REQUEST_TIMEOUT_SECONDS` | HTTP timeout per request | `30` |
| `MAX_RETRIES` | HTTP retry count | `3` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `LOG_FILE` | Log file path | `logs/pipeline.log` |

---

## Known Limitations & Notes

### Groq token cap
The Groq free tier caps at **500K tokens/day** (the binding limit — not the 14,400 requests/day limit). A full run of all 25 sources processes ~400K–600K tokens. If you hit the cap mid-run, switch to Gemini by setting `LLM_PROVIDER=gemini` in `.env` — no code changes needed.

### Content truncation
The LLM only sees the first **8,000 characters** of each page. Long pages like `irs_obbba_legislation` (~15,000–18,000 words) are partially truncated. The most important content is usually near the top, but edge cases near the end of long pages may be missed.

### RenewPACE disabled
`renewpace` (`renewfinancial.com`) is disabled because CloudFront WAF blocks both `requests`-based and Playwright-based scrapers (likely datacenter IP detection). PACE financing for Florida is already covered by `ygrene_pace`.

### `updated_at` nulls are intentional
Many sites do not show a "last updated" date. In those cases `updated_at` is `null` in the output — this is correct behaviour, not a bug. The LLM is explicitly instructed never to fabricate this field.

### Florida Housing Finance Corp — SSL
`floridahousing.org` ships an incomplete TLS certificate chain. The static scraper automatically retries with certificate verification disabled for this site. No sensitive credentials are transmitted; only public marketing HTML is retrieved.

### Duke Energy — JS rendering required
`duke-energy.com` returns HTTP 403 to plain HTTP requests (Akamai WAF / bot detection). The pipeline uses Playwright for both Duke Energy sources. The `networkidle` wait strategy is intentionally avoided because Duke Energy's React app continuously polls analytics endpoints; `domcontentloaded` is used instead.

### DSIRE data scope
The DSIRE API returns **all Florida state programs**, not just Tampa-specific ones. City/county scoping for DSIRE records is done at the LLM extraction stage for HTML sources; for DSIRE direct-mapped records, `city` is always `null` (the API doesn't expose city-level data).

### `review_needed = Yes` does not mean invalid
Records flagged for review are still written to the output. Common reasons include:
- Incentive programs that have no fixed dollar amount (e.g. "contact your utility for details")
- Federal/state programs where the amount depends on individual circumstances
- Programs where the source page is vague or uses non-standard language

Always inspect the `review_needed = Yes` rows in the Excel output before using the data downstream.
