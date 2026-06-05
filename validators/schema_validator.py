"""
Pydantic v2 schema validation for extracted incentive records (spec v2.7 §4).

The main CSV holds only clean rows. Anything missing a required field, with
an unmapped ``incentive_type``, or flagged out-of-scope upstream goes to a
separate quarantine file via ``_quarantine_reasons`` (a private attribute,
never serialised to the main CSV). Records are never silently dropped.
"""
import logging
from typing import Any

from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

from validators.type_normalizer import (
    VALID_INCENTIVE_TYPES,
    normalize_incentive_type,
)

logger = logging.getLogger(__name__)

# Spec §4 column order — no review_needed; includes service_category.
CSV_COLUMNS = [
    "program_name",
    "state",
    "city",
    "zip_code",
    "incentive_type",
    "service_category",
    "property_type",
    "description",
    "eligibility_criteria",
    "incentive_amount",
    "valid_until",
    "updated_at",
    "program_links",
]


class IncentiveRecord(BaseModel):
    program_name: str | None = None
    state: str | None = None
    city: str | None = None
    zip_code: str | None = None
    incentive_type: str | None = None
    service_category: str | None = None
    property_type: str | None = None
    description: str | None = None
    eligibility_criteria: str | None = None
    incentive_amount: str | None = None
    valid_until: str | None = None
    updated_at: str | None = None
    program_links: str | None = None

    # Quarantine reasons live off-schema so they never reach the main CSV.
    # Upstream validators (amount, scope filter, schema) append to this list;
    # OutputWriter splits records on whether the list is non-empty.
    _quarantine_reasons: list[str] = PrivateAttr(default_factory=list)

    model_config = {"str_strip_whitespace": True, "extra": "ignore"}

    @field_validator("incentive_type", mode="before")
    @classmethod
    def _normalise_incentive_type(cls, v):
        if v is None:
            return None
        # normalize_incentive_type returns the canonical value or None.
        # Returning None here triggers the quarantine reason in the model
        # validator below; we preserve the raw value via the reason string.
        normalised = normalize_incentive_type(v)
        return normalised if normalised is not None else v

    @model_validator(mode="after")
    def _compute_quarantine_reasons(self) -> "IncentiveRecord":
        """
        Append quarantine reasons for any required-field or scope failures
        that are detectable from the validated record itself. Upstream stages
        (api_scraper, amount_validator) may have already attached more specific
        reasons via _quarantine_reasons; we skip generic reasons that would be
        redundant noise next to those.
        """
        has_out_of_scope = any(
            r.startswith("out of scope") for r in self._quarantine_reasons
        )
        has_amount_reason = any(
            r.startswith("amount:") for r in self._quarantine_reasons
        )

        if not self.program_name:
            self._quarantine_reasons.append("missing program_name")

        # Skip the generic "missing/unmapped incentive_type" reason when the
        # row was already classified out of scope (the scope reason is more
        # specific and the type is intentionally blank in that case).
        if not has_out_of_scope:
            if not self.incentive_type or self.incentive_type not in VALID_INCENTIVE_TYPES:
                self._quarantine_reasons.append(
                    f"unmapped incentive_type: {self.incentive_type!r}"
                    if self.incentive_type
                    else "missing incentive_type"
                )

        if not self.description or len(self.description.strip()) < 10:
            self._quarantine_reasons.append("missing or too-short description")

        # Skip the generic missing_amount when amount_validator already filed a
        # more specific amount reason for the same row.
        if not self.incentive_amount and not has_amount_reason:
            self._quarantine_reasons.append("missing incentive_amount")

        if self.state and self.state.strip().lower() not in ("florida", "fl", "federal"):
            self._quarantine_reasons.append(f"unexpected state: {self.state}")

        if self._quarantine_reasons:
            logger.debug(
                "quarantine '%s': %s",
                self.program_name or "(unnamed)",
                "; ".join(self._quarantine_reasons),
            )
        return self

    # ── Helpers used by the rest of the pipeline ─────────────────────────────

    @property
    def quarantine_reasons(self) -> list[str]:
        """Public read-only view of the quarantine reasons list."""
        return list(self._quarantine_reasons)

    def add_quarantine_reason(self, reason: str) -> None:
        """Append a reason from an upstream stage (amount validator, scraper)."""
        if reason and reason not in self._quarantine_reasons:
            self._quarantine_reasons.append(reason)

    @property
    def is_quarantined(self) -> bool:
        return bool(self._quarantine_reasons)

    def to_csv_row(self) -> dict:
        """Return dict with exactly the spec §4 CSV columns in order."""
        data = self.model_dump()
        return {col: (data.get(col) or "") for col in CSV_COLUMNS}


def _extract_pre_reasons(raw: dict) -> list[str]:
    """
    Pull and remove the upstream-collected quarantine reasons from a raw dict.
    Earlier stages (amount validator, scope filter) attach reasons via the
    ``_quarantine_reasons`` key so Pydantic ignores it but we can re-attach
    after model construction.
    """
    pre = raw.pop("_quarantine_reasons", None)
    if isinstance(pre, list):
        return [str(r) for r in pre if r]
    return []


def _prune_redundant_reasons(reasons: list[str]) -> list[str]:
    """
    Drop generic reasons that are made redundant by a more specific upstream
    reason on the same record:
      • Any 'out of scope: <type>' subsumes 'missing incentive_type'
        (the type is intentionally blank for out-of-scope rows).
      • Any 'amount: <flag>' subsumes 'missing incentive_amount'
        (the amount validator already filed a specific reason).
    Preserves order.
    """
    has_out_of_scope = any(r.startswith("out of scope") for r in reasons)
    has_amount_flag = any(r.startswith("amount:") for r in reasons)
    pruned: list[str] = []
    for r in reasons:
        if has_out_of_scope and r in ("missing incentive_type",):
            continue
        if has_amount_flag and r == "missing incentive_amount":
            continue
        pruned.append(r)
    return pruned


def validate_record(raw: dict) -> IncentiveRecord | None:
    """
    Validate one raw dict. Returns IncentiveRecord (with quarantine reasons
    already attached) or None on catastrophic parse failure.
    """
    pre_reasons = _extract_pre_reasons(raw)
    try:
        record = IncentiveRecord.model_validate(raw)
    except Exception as exc:
        logger.warning("Pydantic validation failed: %s | error: %s", raw, exc)
        try:
            record = IncentiveRecord(
                program_name=raw.get("program_name"),
                state=raw.get("state"),
                city=raw.get("city"),
                zip_code=raw.get("zip_code"),
                incentive_type=raw.get("incentive_type"),
                service_category=raw.get("service_category"),
                description=raw.get("description"),
                incentive_amount=raw.get("incentive_amount"),
                program_links=raw.get("program_links"),
                updated_at=raw.get("updated_at"),
            )
            record.add_quarantine_reason(f"pydantic fallback: {exc}")
        except Exception:
            return None

    for reason in pre_reasons:
        record.add_quarantine_reason(reason)

    # After all reasons (model-validator + upstream pre_reasons) are attached,
    # collapse redundancies so quarantine.csv shows one specific reason per
    # underlying issue, not a stack of overlapping ones.
    pruned = _prune_redundant_reasons(record._quarantine_reasons)
    record._quarantine_reasons = pruned
    return record


def validate_batch(raw_records: list[dict], source_id: str = "") -> tuple[list[IncentiveRecord], int]:
    """
    Validate a list of raw dicts. Returns (validated_records, failed_count).
    Records may or may not have quarantine reasons attached; callers split on
    ``record.is_quarantined``.
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

    quarantined = sum(1 for r in validated if r.is_quarantined)
    logger.info(
        "Validation [%s]: %d records, %d clean, %d quarantine, %d unparseable",
        source_id or "?",
        len(validated),
        len(validated) - quarantined,
        quarantined,
        failed,
    )
    return validated, failed
