"""
Tampa Incentive Data Extraction Pipeline
=========================================
Main entry point. Orchestrates: Discover → Scrape → Parse (LLM) → Validate → CSV output.

Usage:
    python pipeline.py                           # Run all enabled sources
    python pipeline.py --priority P0             # Run P0 sources only
    python pipeline.py --source teco_rebates     # Run a single source by ID
    python pipeline.py --dry-run                 # Parse but don't write CSV
    python pipeline.py --priority P0 --dry-run   # Quick test without output
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing anything that reads env vars
load_dotenv()


def _setup_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "logs/pipeline.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format=fmt, handlers=handlers)


_setup_logging()
logger = logging.getLogger(__name__)

from agents.discoverer import Discoverer
from agents.output_writer import OutputWriter
from parsers.llm_parser import LLMParser
from parsers.table_parser import parse_tables
from validators.schema_validator import IncentiveRecord, validate_batch
from validators.amount_validator import apply_amount_validation_batch
from validators.zip_enricher import enrich_zip_codes
from validators.text_sanitizer import sanitize_batch


@dataclass
class PipelineStats:
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    records_extracted: int = 0
    records_validated: int = 0
    records_flagged_review: int = 0
    validation_failures: int = 0
    errors: list[str] = field(default_factory=list)


def run_pipeline(
    priority_filter: str | None = None,
    source_filter: str | None = None,
    group_filter: str | None = None,
    dry_run: bool = False,
    output_path: str = "output/extracted_tampa_incentives.csv",
) -> PipelineStats:
    stats = PipelineStats()
    all_records: list[IncentiveRecord] = []
    records_by_source: dict[str, list[IncentiveRecord]] = {}

    # ── Step 1: Load sources ──────────────────────────────────────────────────
    discoverer = Discoverer()
    sources = discoverer.load_sources(
        priority_filter=priority_filter,
        source_filter=source_filter,
        group_filter=group_filter,
    )

    if not sources:
        logger.warning("No sources matched the given filters. Nothing to process.")
        return stats

    logger.info("Pipeline starting — %d sources to process", len(sources))

    # ── Step 2: Init LLM parser (validates API key early) ─────────────────────
    try:
        parser = LLMParser()
    except EnvironmentError as exc:
        logger.error("Cannot start pipeline: %s", exc)
        sys.exit(1)

    writer = OutputWriter(output_path=output_path)

    # ── Step 3: Process each source ───────────────────────────────────────────
    for i, source in enumerate(sources, start=1):
        source_id = source.get("id", f"source_{i}")
        source_name = source.get("name", source_id)
        priority = source.get("priority", "?")
        logger.info("[%d/%d] Processing [%s] %s", i, len(sources), priority, source_name)

        stats.sources_attempted += 1

        # 3a. Scrape
        try:
            scraper = discoverer.get_scraper_for_source(source)
            raw_content = scraper.fetch(source)
        except Exception as exc:
            logger.error("Unexpected scraper error for %s: %s", source_id, exc)
            stats.sources_failed += 1
            stats.errors.append(f"{source_id}: scraper error — {exc}")
            continue

        if not raw_content.success:
            logger.warning("Skipping %s — fetch failed: %s", source_id, raw_content.error_message)
            stats.sources_failed += 1
            stats.errors.append(f"{source_id}: {raw_content.error_message}")
            continue

        stats.sources_succeeded += 1

        extraction_method = source.get("extraction_method", "llm")

        # 3b. Route to the correct parser based on extraction_method ──────────
        #
        #   direct  → already structured (DSIRE API): use parsed_records, skip all parsers
        #   table   → HTML tables present: BS4 table parser (no LLM)
        #             falls back to LLM automatically if no usable tables found
        #   llm     → prose / inconsistent layout: send to Groq/Gemini

        if extraction_method == "direct" or raw_content.parsed_records:
            # Structured data already mapped (e.g. DSIRE)
            raw_records = raw_content.parsed_records
            logger.info("  [direct] %d pre-mapped records from %s — LLM skipped", len(raw_records), source_id)

        elif extraction_method == "table":
            # Try HTML table parser first
            raw_records = parse_tables(raw_content.raw_html or "", source)
            if raw_records:
                logger.info("  [table]  %d records from %s — LLM skipped", len(raw_records), source_id)
            else:
                # No usable tables found — fall back to LLM
                logger.info("  [table→llm] No tables in %s — falling back to LLM", source_id)
                raw_records = parser.parse(raw_content)

        else:
            # llm (default) — JS pages, PDFs, prose content
            raw_records = parser.parse(raw_content)

        stats.records_extracted += len(raw_records)

        if not raw_records:
            logger.info("No incentives found for %s", source_id)
            continue

        # 3c. Rule-based amount validation (fast, before Pydantic)
        raw_records = apply_amount_validation_batch(raw_records)

        # 3c.3: Strip URLs/links from description and eligibility_criteria so
        # the dashboard never renders external CTAs in narrative fields. The
        # canonical apply link stays in program_links.
        raw_records = sanitize_batch(raw_records)

        # 3c.5: ZIP enrichment — fill zip_code from city/county scope when the
        # LLM returned null (most program pages don't list ZIPs verbatim, but
        # "Tampa" or "Hillsborough County" deterministically implies a ZIP set).
        raw_records = enrich_zip_codes(raw_records)

        # 3d. Pydantic schema validation
        validated, failed = validate_batch(raw_records, source_id=source_id)
        stats.records_validated += len(validated)
        stats.validation_failures += failed
        stats.records_flagged_review += sum(1 for r in validated if r.review_needed == "Yes")
        all_records.extend(validated)
        # Track records by sheet group for Excel output.
        # Sources can opt into grouping by setting `sheet_group:` in sources.yaml
        # (e.g. all hillsborough_* sources -> "Hillsborough County" sheet).
        # Without sheet_group, each source gets its own sheet.
        sheet_key = source.get("sheet_group") or source_id
        records_by_source.setdefault(sheet_key, []).extend(validated)

        logger.info(
            "  -> %d extracted, %d validated (%d need review)",
            len(raw_records),
            len(validated),
            sum(1 for r in validated if r.review_needed == "Yes"),
        )

        # Polite delay between sources
        rate_limit = source.get("rate_limit_seconds", 1.0)
        if i < len(sources):
            time.sleep(rate_limit)

    # ── Step 4: Deduplicate ───────────────────────────────────────────────────
    deduped = writer.deduplicate(all_records)

    # ── Step 5: Write CSV + multi-sheet XLSX ──────────────────────────────────
    if dry_run:
        logger.info("Dry run — skipping output write (%d records would be written)", len(deduped))
    else:
        writer.write(deduped)
        writer.write_xlsx(records_by_source=records_by_source, combined=deduped)

    return stats


def _print_summary(stats: PipelineStats, dry_run: bool) -> None:
    print("\n" + "=" * 55)
    print("  PIPELINE SUMMARY")
    print("=" * 55)
    print(f"  Sources processed : {stats.sources_succeeded}/{stats.sources_attempted}")
    if stats.sources_failed:
        print(f"  Sources failed    : {stats.sources_failed}")
    print(f"  Records extracted : {stats.records_extracted}")
    print(f"  Records validated : {stats.records_validated}")
    print(f"  Need review       : {stats.records_flagged_review}")
    if stats.validation_failures:
        print(f"  Parse failures    : {stats.validation_failures}")
    if dry_run:
        print("  Output            : (dry run — no file written)")
    else:
        print("  Output (CSV)      : output/extracted_tampa_incentives.csv")
        print("  Output (XLSX)     : output/extracted_tampa_incentives.xlsx")
        print("                      (multi-sheet: All Records + one per source group)")
    if stats.errors:
        print(f"\n  Errors ({len(stats.errors)}):")
        for err in stats.errors[:10]:
            print(f"    - {err}")
        if len(stats.errors) > 10:
            print(f"    ... and {len(stats.errors) - 10} more (see logs/pipeline.log)")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description="Tampa Incentive Data Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    arg_parser.add_argument("--priority", choices=["P0", "P1", "P2"], help="Run only this priority tier")
    arg_parser.add_argument("--source", type=str, help="Run only this source ID (e.g. teco_rebates)")
    arg_parser.add_argument("--group", type=str, help="Run only sources in this sheet group (e.g. 'Hillsborough County')")
    arg_parser.add_argument("--dry-run", action="store_true", help="Fetch and parse but do not write output CSV")
    arg_parser.add_argument(
        "--output",
        type=str,
        default="output/extracted_tampa_incentives.csv",
        help="Output CSV path (default: output/extracted_tampa_incentives.csv)",
    )
    args = arg_parser.parse_args()

    stats = run_pipeline(
        priority_filter=args.priority,
        source_filter=args.source,
        group_filter=args.group,
        dry_run=args.dry_run,
        output_path=args.output,
    )
    _print_summary(stats, dry_run=args.dry_run)
