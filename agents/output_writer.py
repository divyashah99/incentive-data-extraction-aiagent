"""
OutputWriter — writes validated IncentiveRecord objects to CSV and/or Excel.

CSV output: single file, all deduplicated records, one row per record.
Excel output: multi-sheet workbook with one sheet per source + a combined
"All Records" sheet. Useful for browsing which records came from which source.
"""
import csv
import logging
import os
import re

from validators.schema_validator import IncentiveRecord, CSV_COLUMNS

logger = logging.getLogger(__name__)

# Excel sheet names are limited to 31 chars and forbid these characters
_INVALID_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")


def _safe_sheet_name(name: str, used: set[str]) -> str:
    """Make a sheet name Excel-safe (≤31 chars, no forbidden chars, unique)."""
    clean = _INVALID_SHEET_CHARS.sub(" ", name).strip()[:31] or "Sheet"
    if clean not in used:
        used.add(clean)
        return clean
    # Disambiguate with a suffix
    for i in range(2, 100):
        suffix = f" ({i})"
        candidate = clean[: 31 - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
    used.add(clean)
    return clean


class OutputWriter:

    def __init__(self, output_path: str = "output/extracted_tampa_incentives.csv"):
        self.output_path = output_path

    def write(self, records: list[IncentiveRecord]) -> int:
        """Write all records to CSV (overwrite mode). Returns number of rows written."""
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)

        with open(self.output_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record.to_csv_row())

        logger.info("Wrote %d records to %s", len(records), self.output_path)
        return len(records)

    def append(self, records: list[IncentiveRecord]) -> int:
        """Append records to existing CSV (creates file if not present)."""
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        file_exists = os.path.exists(self.output_path)

        with open(self.output_path, "a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for record in records:
                writer.writerow(record.to_csv_row())

        logger.info("Appended %d records to %s", len(records), self.output_path)
        return len(records)

    def write_xlsx(
        self,
        records_by_source: dict[str, list[IncentiveRecord]],
        combined: list[IncentiveRecord],
        path: str | None = None,
    ) -> int:
        """
        Write a multi-sheet Excel workbook.
          • Sheet "All Records" first — combined deduplicated records
          • One sheet per source (named after the source's display name or id)

        Returns the total number of records across all per-source sheets.
        """
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        xlsx_path = path or self.output_path.replace(".csv", ".xlsx")
        os.makedirs(os.path.dirname(xlsx_path) or ".", exist_ok=True)

        wb = Workbook()
        wb.remove(wb.active)  # drop default sheet

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="305496")
        review_fill = PatternFill("solid", fgColor="FFF2CC")  # soft yellow
        wrap = Alignment(wrap_text=True, vertical="top")

        def _add_sheet(sheet_name: str, records: list[IncentiveRecord]) -> None:
            ws = wb.create_sheet(title=sheet_name)
            ws.append(CSV_COLUMNS)
            # Style header
            for col_idx in range(1, len(CSV_COLUMNS) + 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            # Data rows
            review_col_idx = CSV_COLUMNS.index("review_needed") + 1
            for record in records:
                row = record.to_csv_row()
                ws.append([row.get(c, "") for c in CSV_COLUMNS])
                r = ws.max_row
                # Highlight rows that need review
                if (row.get("review_needed") or "").lower() == "yes":
                    for col_idx in range(1, len(CSV_COLUMNS) + 1):
                        ws.cell(row=r, column=col_idx).fill = review_fill
                # Wrap long text cells
                for col_idx in range(1, len(CSV_COLUMNS) + 1):
                    ws.cell(row=r, column=col_idx).alignment = wrap

            # Column widths (sensible per-column defaults; not auto-fit to stay fast)
            widths = {
                "program_name": 40, "state": 10, "city": 18, "zip_code": 16,
                "incentive_type": 18, "property_type": 22, "description": 60,
                "eligibility_criteria": 45, "incentive_amount": 28,
                "valid_until": 14, "updated_at": 14, "review_needed": 12,
                "program_links": 40,
            }
            for i, col in enumerate(CSV_COLUMNS, start=1):
                ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 20)
            ws.freeze_panes = "A2"

        # 1. Combined sheet first
        used_names: set[str] = set()
        all_sheet = _safe_sheet_name("All Records", used_names)
        _add_sheet(all_sheet, combined)

        # 2. One sheet per source (sorted by source id for stable order)
        per_source_total = 0
        for source_id in sorted(records_by_source.keys()):
            recs = records_by_source[source_id]
            if not recs:
                continue
            sheet_name = _safe_sheet_name(source_id, used_names)
            _add_sheet(sheet_name, recs)
            per_source_total += len(recs)

        wb.save(xlsx_path)
        logger.info(
            "Wrote XLSX: %s (%d combined records, %d sheets per source)",
            xlsx_path, len(combined), len(records_by_source),
        )
        return per_source_total

    def deduplicate(self, records: list[IncentiveRecord]) -> list[IncentiveRecord]:
        """
        Remove duplicates keyed on (program_name, incentive_type) — case-insensitive.
        When duplicates exist, prefers the record with review_needed = "No".
        """
        seen: dict[tuple, IncentiveRecord] = {}

        for record in records:
            key = (
                (record.program_name or "").lower().strip(),
                (record.incentive_type or "").lower().strip(),
            )
            if key not in seen:
                seen[key] = record
            else:
                existing = seen[key]
                # Prefer the record with no review flags
                if record.review_needed == "No" and existing.review_needed == "Yes":
                    seen[key] = record

        deduped = list(seen.values())
        dropped = len(records) - len(deduped)
        if dropped:
            logger.info("Deduplication removed %d duplicate(s)", dropped)
        return deduped
