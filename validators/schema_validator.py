"""
Pydantic v2 schema validation for extracted incentive records.

review_needed is set purely by rule — no LLM confidence score involved.
Rule: mark "Yes" if any field that MUST be present to be actionable is
missing or invalid. Fields that are legitimately absent for many programs
(city, property_type, eligibility_criteria, valid_until, updated_at) do
NOT trigger a flag on their own.

Records are never dropped — always included with review_needed set.
"""
import logging
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)

VALID_INCENTIVE_TYPES = {"Grants", "Rebates", "Finance Solutions", "Tax Credits", "Investments"}

# Exact CSV column order from spec
CSV_COLUMNS = [
    "program_name",
    "state",
    "city",
    "incentive_type",
    "property_type",
    "description",
    "eligibility_criteria",
    "incentive_amount",
    "valid_until",
    "updated_at",
    "review_needed",
    "program_links",
]


class IncentiveRecord(BaseModel):
    program_name: str | None = None
    state: str | None = None
    city: str | None = None
    incentive_type: str | None = None
    property_type: str | None = None
    description: str | None = None
    eligibility_criteria: str | None = None
    incentive_amount: str | None = None
    valid_until: str | None = None
    updated_at: str | None = None
    review_needed: Literal["Yes", "No"] = "No"
    program_links: str | None = None

    model_config = {"str_strip_whitespace": True, "extra": "ignore"}

    @field_validator("incentive_type", mode="before")
    @classmethod
    def normalise_incentive_type(cls, v):
        if v is None:
            return None
        # Accept minor case variations, e.g. "tax credits" → "Tax Credits"
        for valid in VALID_INCENTIVE_TYPES:
            if v.strip().lower() == valid.lower():
                return valid
        return v  # keep as-is; model_validator will flag it

    @model_validator(mode="after")
    def compute_review_needed(self) -> "IncentiveRecord":
        """
        Pure rule-based review flag. Fires when any field that makes a record
        actionable is missing or invalid. Deliberately does NOT flag absence of
        optional fields (city, property_type, eligibility_criteria, valid_until,
        updated_at) — those are legitimately null for many programs.
        """
        flags: list[str] = []

        # 1. Program must have an identifiable name
        if not self.program_name:
            flags.append("missing program_name")

        # 2. Incentive type must map to one of the 5 valid categories
        if not self.incentive_type or self.incentive_type not in VALID_INCENTIVE_TYPES:
            flags.append("missing or invalid incentive_type")

        # 3. Description must have meaningful content
        if not self.description or len(self.description.strip()) < 10:
            flags.append("missing or too-short description")

        # 4. Incentive amount is the core data point — flag if absent
        if not self.incentive_amount:
            flags.append("missing incentive_amount")

        # 5. State should be Florida (or null for unresolved) — flag if explicitly wrong
        if self.state and self.state.strip().lower() not in ("florida", "fl", "federal"):
            flags.append(f"unexpected state: {self.state}")

        if flags:
            self.review_needed = "Yes"
            logger.debug(
                "review_needed=Yes for '%s': %s",
                self.program_name or "(unnamed)", "; ".join(flags),
            )

        return self

    def to_csv_row(self) -> dict:
        """Return dict with exactly the CSV columns in the required order."""
        data = self.model_dump()
        return {col: (data.get(col) or "") for col in CSV_COLUMNS}


def validate_record(raw: dict) -> IncentiveRecord | None:
    """
    Validate one raw dict. Returns IncentiveRecord (review_needed already set
    by the model_validator) or None on catastrophic parse failure.
    """
    try:
        return IncentiveRecord.model_validate(raw)
    except Exception as exc:
        logger.warning("Pydantic validation failed: %s | error: %s", raw, exc)
        # Last-resort: preserve whatever fields we can
        try:
            return IncentiveRecord(
                program_name=raw.get("program_name"),
                state=raw.get("state"),
                city=raw.get("city"),
                incentive_type=raw.get("incentive_type"),
                description=raw.get("description"),
                incentive_amount=raw.get("incentive_amount"),
                program_links=raw.get("program_links"),
                updated_at=raw.get("updated_at"),
                review_needed="Yes",
            )
        except Exception:
            return None


def validate_batch(raw_records: list[dict], source_id: str = "") -> tuple[list[IncentiveRecord], int]:
    """
    Validate a list of raw dicts. Returns (validated_records, failed_count).
    confidence_score is intentionally ignored — review_needed is rule-based only.
    """
    validated: list[IncentiveRecord] = []
    failed = 0

    for raw in raw_records:
        record = validate_record(raw)
        if record is not None:
            validated.append(record)
        else:
            failed += 1
            logger.warning("Dropped unparseable record from '%s': %s", source_id, raw)

    logger.info(
        "Validation [%s]: %d validated, %d failed, %d need review",
        source_id or "?",
        len(validated),
        failed,
        sum(1 for r in validated if r.review_needed == "Yes"),
    )
    return validated, failed
