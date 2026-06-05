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
  │  rule-based               appends quarantine reasons │
  └────────────────────────┬────────────────────────────┘
                           |
                    list[IncentiveRecord]
                           |
                           v
  ┌─────────────────────────────────────────────────────┐
  │                   OUTPUT LAYER                       │
  │                                                      │
  │  OutputWriter                                        │
  │  split clean/quarantined →                           │
  │    write() + write_quarantine() +                    │
  │    write_program_geo() + write_xlsx()                │
  └─────────────────────────────────────────────────────┘
         |              |              |          |
         v              v              v          v
  extracted_      quarantine.csv  program_geo  extracted_
  tampa_inc.csv   (reasons)       .csv         tampa_inc.xlsx
  (Layer 1)                       (Layer 2)    (review aid)
```

---

## Project Structure

```
incentive-data-extraction-aiagent/
│
├── config/
│   └── sources.yaml          # Source registry (spec §9.5 fields: status, scope, scrape_notes …)
│
├── geo/
│   └── geo_crosswalk_tampa_hillsborough.csv   # city ↔ zip ↔ county (spec §5.4)
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
│   ├── schema_validator.py   # Pydantic v2 model + quarantine reasons
│   ├── type_normalizer.py    # Canonical incentive_type values + alias map
│   ├── amount_validator.py   # Rule-based amount normalisation & sanity checks
│   ├── text_sanitizer.py     # Strip URLs from narrative fields
│   └── zip_enricher.py       # City/county → ZIPs from crosswalk
│
├── agents/
│   ├── discoverer.py         # Reads sources.yaml (status filter), scraper factory
│   └── output_writer.py      # Layer 1 CSV + quarantine.csv + program_geo.csv + XLSX
│
├── pipeline.py               # Main entry point (CLI)
├── requirements.txt
├── .env                      # API keys and settings (never commit)
├── .env.example              # Template
├── .gitignore
├── logs/                     # Created at runtime — pipeline.log
└── output/                   # Created at runtime — handoff package CSVs + XLSX
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

Flagged records get a quarantine reason appended (e.g. `amount: > $10M`) which is carried alongside the record through Pydantic into `quarantine.csv`.

#### Schema Validator (`validators/schema_validator.py`)

Pydantic v2 `BaseModel` whose `@model_validator` appends a quarantine reason for any of the following:

| Condition | Reason text |
|---|---|
| `program_name` is null or empty | `missing program_name` |
| `incentive_type` not in the 5 valid types | `unmapped incentive_type: '<raw>'` |
| `description` is null or < 10 characters | `missing or too-short description` |
| `incentive_amount` is null | `missing incentive_amount` |
| `state` is set but not Florida/FL/Federal | `unexpected state: <value>` |

**Records are never silently dropped.** Clean rows land in the main CSV; quarantined rows land in `quarantine.csv` with their reasons. Optional fields (`city`, `service_category`, `property_type`, `eligibility_criteria`, `valid_until`, `updated_at`) do not trigger quarantine on their own — they are legitimately absent for many programs.

Upstream stages (`scrapers/api_scraper.py`, `validators/amount_validator.py`) attach reasons via a `_quarantine_reasons` list on the raw dict; the schema validator merges those onto the model so any single record carries the union of every reason it accumulated.

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

Before writing, `OutputWriter.deduplicate()` removes duplicate records keyed on `(program_name, incentive_type)` (case-insensitive). When duplicates exist — for example, DSIRE and a utility site both list the same program — the record with no quarantine reasons is kept.

#### Handoff package (spec §6.4)

All three files are written UTF-8 BOM (`utf-8-sig`) so Excel opens them without an import wizard.

| File | Contents |
|---|---|
| `extracted_tampa_incentives.csv` | Clean Layer 1 rows, exactly the 13 spec §4 columns in spec order. |
| `quarantine.csv` | Quarantined rows: 13 spec columns + `quarantine_reason`. |
| `program_geo.csv` | Layer 2 (spec §5.2): `program_id, geo_type, geo_value`. Built from clean rows only, after validation. |

#### Excel Output (multi-sheet)

The XLSX workbook is for human review and merges clean + quarantined into one sheet per source plus an "All Records" sheet. Quarantined rows are highlighted yellow and the `quarantine_reason` column makes the reason visible at a glance.

Formatting:
- Dark blue bold header row
- Soft yellow fill for quarantined rows
- Frozen top row
- Pre-set column widths and word-wrapped cells

---

## Data Sources

Spec §9 treats the registry as an open set — coverage grows over time, not via a fixed "done list". The authoritative list of sources is `config/sources.yaml`; this README does not duplicate it. To see what runs today:

```bash
python -c "import yaml; d=yaml.safe_load(open('config/sources.yaml','r',encoding='utf-8')); [print(f\"{s['status']:11} {s['id']:36} {s.get('scope','-'):8} {s['name']}\") for s in d['sources']]"
```

A handful of category notes that are not obvious from the YAML:

- **DSIRE (`dsire_florida`)** — paginated JSON API, direct field mapping (no LLM). Returns *all* Florida programs in the DSIRE index, including federal programs linked from Florida. Non-incentive types (Net Metering, Building Energy Code, RPS, etc.) are filtered to `quarantine.csv` per spec §3.1.
- **TECO / Duke Energy** — utility rebate pages. Duke requires Playwright (Akamai WAF).
- **IRS 25C / 25D** — federal residential tax credits.
- **Florida Housing Finance Corp** — SHIP, Hometown Heroes, DPA programs.
- **Hillsborough County** — SHIP/CDBG/HOME-administered local programs + HRRP disaster recovery (Power Pages site, Playwright-rendered with accordion expansion).
- **My Safe Florida Home / FEMA / Ygrene** — disaster resilience and PACE financing.
- **`renewpace`** — kept as `status: deprecated` (CloudFront WAF blocks Playwright); PACE financing already covered by `ygrene_pace`.

---

## Output Schema

The pipeline produces three CSV files per run (spec v2.7 §6.4 Phase 0 handoff):

| File | Role |
|---|---|
| `output/extracted_tampa_incentives.csv` | **Layer 1** — clean rows in the spec §4 column order |
| `output/quarantine.csv` | Failed / out-of-scope rows + `quarantine_reason` (replaces the old `review_needed` column) |
| `output/program_geo.csv` | **Layer 2** — `program_id, geo_type, geo_value` search index (spec §5.2) |

### Layer 1 columns (`extracted_tampa_incentives.csv`)

| # | Column | Description | Example |
|---|---|---|---|
| 1 | `program_name` | Official program name | `TECO Ductwork Rebate` |
| 2 | `state` | Full state name (`Florida`) or `All` for federal/nationwide | `Florida` |
| 3 | `city` | `Tampa`, `Hillsborough County`, or null for statewide/federal | `Tampa` |
| 4 | `zip_code` | ZIPs verbatim from source; semicolon-separated for multiple | `33602;33603` |
| 5 | `incentive_type` | EXACTLY one of the 5 spec values (see §4.2) | `Rebates` |
| 6 | `service_category` | Home-upgrade categories the program funds, as worded on source | `HVAC;Weatherization` |
| 7 | `property_type` | As stated in source | `Residential` |
| 8 | `description` | Plain-narrative summary of what the program funds | `Cash rebate for...` |
| 9 | `eligibility_criteria` | Who qualifies — verbatim from the source | `Homeowners in TECO service area...` |
| 10 | `incentive_amount` | Amount as stated in source | `$125 per unit` |
| 11 | `valid_until` | Expiry date if stated, text OK (`Ongoing`, `Rolling`) | `2032-12-31` |
| 12 | `updated_at` | Last-updated date from source page (never today's date) | `2024-03-01` |
| 13 | `program_links` | Direct URL to apply or learn more | `https://...` |

`updated_at` is extracted verbatim from the source page. It is **never** set to today's scrape date — if no update date is visible, the field is `null`.

### Quarantine reasons (`quarantine.csv`)

The 13 Layer 1 columns plus a trailing `quarantine_reason` column. Common reasons:

| Reason | Source |
|---|---|
| `out of scope: <DSIRE type>` | DSIRE row whose `typeObj.name` is a regulation/code/standard (spec §3.1) |
| `unmapped incentive_type: '<raw>'` | LLM or table returned a label outside the 5 spec values |
| `missing incentive_amount` | No amount visible on the source page |
| `missing or too-short description` | Source provided no usable summary |
| `amount: <flag>` | Amount validator sanity check failed ($0, > $10M, > 100%, etc.) |

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

### Full run (all active sources)

```bash
python pipeline.py
```

### Single source

```bash
python pipeline.py --source teco_ductwork
python pipeline.py --source dsire_florida
```

`--source` bypasses the status filter, so `candidate` and `deprecated` sources can still be exercised by id when iterating on a new domain.

### Source group

```bash
python pipeline.py --group "Tampa Electric (TECO)"
```

### Dry run (fetch + parse, no output files written)

```bash
python pipeline.py --dry-run
```

### Custom output path

```bash
python pipeline.py --output data/tampa_incentives_2026.csv
```

### Output

After a successful run:

```
output/
├── extracted_tampa_incentives.csv   # Layer 1, clean rows only (spec §4)
├── quarantine.csv                   # Failed/out-of-scope rows + reason
├── program_geo.csv                  # Layer 2 search index (spec §5.2)
└── extracted_tampa_incentives.xlsx  # Multi-sheet workbook for review
    ├── All Records                  # Combined (clean + quarantined, yellow = quarantined)
    └── ...                          # One sheet per source / sheet_group
```

Logs are written to `logs/pipeline.log` and also printed to stdout.

### Pipeline summary (printed after every run)

```
=======================================================
  PIPELINE SUMMARY
=======================================================
  Sources processed : 25/26
  Sources failed    : 1
  Records extracted : 141
  Records validated : 141
  Quarantined       : 28
  Output (Layer 1)  : output/extracted_tampa_incentives.csv
  Output (quarant.) : output/quarantine.csv
  Output (Layer 2)  : output/program_geo.csv
  Output (XLSX)     : output/extracted_tampa_incentives.xlsx
=======================================================
```

---

## Configuration Reference

### `config/sources.yaml` fields (spec v2.7 §9.5)

The registry is an **open set** sorted by operational needs, not a static priority ranking. There is no `priority` / `P0` field — sources are gated by `status:` only.

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique snake_case identifier |
| `name` | yes | Human-readable display name |
| `status` | yes | `active` (runs), `candidate` (under evaluation, skipped unless `--source` names it), `deprecated` (skipped, kept for audit) |
| `type` | yes | `static_html`, `js_rendered`, `api`, or `pdf` |
| `extraction_method` | yes | `direct`, `table`, or `llm` |
| `scope` | yes | `energy`, `disaster`, or `both` (spec §3.1) |
| `format` | yes | `html`, `pdf`, `table`, or `mixed` |
| `refresh_cadence` | yes | How often the source is rechecked (`weekly`, `monthly`, …) |
| `discovered_at` | yes | ISO date the source was added |
| `discovered_by` | yes | `human`, `pipeline`, or `agent` |
| `owner` | yes | Team or individual responsible |
| `scrape_notes` | yes | Free-text notes on auth, quirks, brittleness, what the LLM needs to know |
| `url` | for HTML/JS | Target page URL |
| `base_url` | for API | API base endpoint |
| `api_params` | for API | Query params dict |
| `rate_limit_seconds` | no | Min seconds between requests (default: 1.0) |
| `js_wait_selector` | JS only | CSS selector to wait for before extracting |
| `js_wait_seconds` | JS only | Extra settle time in seconds (default: 0) |
| `js_wait_until` | JS only | Playwright wait event override (default: `domcontentloaded`) |
| `js_expand_accordions` | JS only | Expand collapsed `<details>` / Bootstrap accordions before extract |
| `sheet_group` | no | XLSX sheet grouping for related sources |

### Adding a New Source

The registry is open by design (spec §9). To add a new source:

**1. Append an entry to `config/sources.yaml`** with `status: candidate`:

```yaml
  - id: my_new_source
    name: "City of Foo — Energy Rebate Program"
    status: candidate                  # promote to `active` once rows look right
    type: static_html                  # or js_rendered / api / pdf
    extraction_method: llm             # or direct / table
    scope: energy                      # or disaster / both
    format: html                       # or pdf / table / mixed
    refresh_cadence: weekly
    discovered_at: 2026-06-03
    discovered_by: human
    owner: scraper-team
    url: "https://example.gov/rebates"
    rate_limit_seconds: 1.0
    scrape_notes: >
      What you learned while reading the page — auth requirements, layout
      quirks, dates/amounts to double-check, anything brittle.
```

**2. Test with just this source**:

```bash
python pipeline.py --source my_new_source
```

The `--source` flag overrides the status filter, so candidate sources can still be exercised by id. Inspect:

- `output/extracted_tampa_incentives.csv` — did clean rows come through?
- `output/quarantine.csv` — what got flagged, and why?
- `output/extracted_tampa_incentives.xlsx` — yellow-highlighted rows = quarantined.

**3. Promote when satisfied** by changing `status: candidate` → `status: active`.

**4. Full rerun**:

```bash
python pipeline.py
```

This regenerates all three handoff artifacts (Layer 1 CSV, `quarantine.csv`, `program_geo.csv`) using every active source.

**No code changes** are needed for new sources of `type: static_html | js_rendered | api | pdf`. Only domain-specific quirks (custom auth, unusual JSON shapes, anti-bot measures) require touching the matching scraper in `scrapers/`.

### Deprecating a Source

Set `status: deprecated` and add a `scrape_notes` line explaining why (anti-bot, dead domain, duplicate, etc.). Deprecated sources stay in the YAML for audit — never delete entries.

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

### Quarantine is not a synonym for invalid
Rows in `quarantine.csv` are not necessarily wrong — they failed a validation rule. Common reasons:
- DSIRE returned a regulation or policy (Net Metering, Building Energy Code) — out of scope per spec §3.1.
- Source page has no fixed dollar amount (e.g. "contact your utility for details").
- Federal/state programs where amount depends on individual circumstances.
- LLM extracted an `incentive_type` outside the 5 spec values that the alias map didn't recognise.

Always inspect `quarantine.csv` (or the yellow-highlighted rows in the XLSX) before treating the Layer 1 CSV as complete.
