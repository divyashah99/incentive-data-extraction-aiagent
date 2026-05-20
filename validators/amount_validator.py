"""
Rule-Based Amount Validator
───────────────────────────
From the brief: "Amount validation / sanity check → Rule-based validator.
Rules are faster and more reliable for numbers."

Does three things:
  1. NORMALISE  — clean up inconsistent formatting ($300.00 → $300, "30 %" → "30%")
  2. EXTRACT    — pull the numeric value(s) out for sanity checking
  3. FLAG       — mark suspicious amounts for human review (review_needed = "Yes")

Plugs into the pipeline AFTER LLM extraction and Pydantic validation.
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Matches dollar amounts: $300, $1,200, $10,000.50, $1.2M
_DOLLAR_RE = re.compile(
    r"\$\s*(?P<val>[\d,]+(?:\.\d+)?)\s*(?P<mult>[MmKk])?",
    re.I,
)
# Matches percentages: 30%, 30 %, 30.5%
_PCT_RE = re.compile(r"(?P<val>\d+(?:\.\d+)?)\s*%")

# Matches "up to", "max", "maximum" qualifiers
_UPTO_RE  = re.compile(r"\bup\s+to\b|\bmaximum\b|\bmax\b|\bup\sto\b", re.I)
_PERYEAR  = re.compile(r"\bper\s+year\b|\bper\s+yr\b|\bannually\b|\b/\s*yr\b|\b/\s*year\b", re.I)

# Sanity thresholds
_MAX_REASONABLE_DOLLAR = 10_000_000   # $10M — above this is almost certainly wrong
_MIN_REASONABLE_DOLLAR = 1            # $0 amounts are suspicious (but not per-unit rates)

# Matches per-unit qualifiers — these make small dollar amounts legitimate
_PER_UNIT_RE = re.compile(
    r"\bper\b|/\s*(?:kw|kwh|wh|sq\.?\s*ft|sqft|unit|gallon|therm|year|yr|ton|btu)",
    re.I,
)


@dataclass
class AmountValidationResult:
    original: str | None
    normalised: str | None
    numeric_value: float | None      # primary extracted dollar or percentage value
    is_percentage: bool
    is_per_year: bool
    is_up_to: bool
    flag: bool                        # True = needs human review
    flag_reason: str | None


def _parse_multiplier(mult: str) -> float:
    if mult and mult.upper() == "M":
        return 1_000_000
    if mult and mult.upper() == "K":
        return 1_000
    return 1.0


def validate_amount(raw_amount: str | None) -> AmountValidationResult:
    """
    Validate and normalise a raw incentive_amount string.
    Returns AmountValidationResult with a flag if something looks wrong.
    """
    if not raw_amount or not raw_amount.strip():
        return AmountValidationResult(
            original=raw_amount,
            normalised=None,
            numeric_value=None,
            is_percentage=False,
            is_per_year=False,
            is_up_to=False,
            flag=True,
            flag_reason="missing incentive_amount",
        )

    text = raw_amount.strip()
    is_upto   = bool(_UPTO_RE.search(text))
    is_peryear = bool(_PERYEAR.search(text))

    # ── Multi-tier amount? Preserve as-is ───────────────────────────────────
    # Strings like "Up to $150,000 (repair); Up to $350,000 (reconstruction);
    # Up to $50,000 (reimbursement)" carry essential program detail. Single-value
    # normalisation would silently drop all tiers but the first. We only sanity-
    # check the largest value, then return the string untouched.
    dollar_matches = list(_DOLLAR_RE.finditer(text))
    if len(dollar_matches) > 1:
        values: list[float] = []
        for m in dollar_matches:
            try:
                v = float(m.group("val").replace(",", "")) * _parse_multiplier(m.group("mult") or "")
                values.append(v)
            except (ValueError, TypeError):
                continue
        max_val = max(values) if values else None

        # Sanity-check the largest tier (suspiciously high catches LLM parse errors
        # like "$10,000,000,000"). We don't flag low values here because per-unit
        # rates are common inside multi-tier strings.
        flag, reason = False, None
        if max_val is not None and max_val > _MAX_REASONABLE_DOLLAR:
            flag = True
            reason = f"suspiciously high amount: ${max_val:,.0f} — verify"

        return AmountValidationResult(
            original=raw_amount,
            normalised=text,           # KEEP the multi-tier string intact
            numeric_value=max_val,
            is_percentage=False,
            is_per_year=is_peryear,
            is_up_to=is_upto,
            flag=flag,
            flag_reason=reason,
        )

    # ── Try dollar amount first ─────────────────────────────────────────────
    dollar_match = _DOLLAR_RE.search(text)
    if dollar_match:
        raw_val  = dollar_match.group("val").replace(",", "")
        mult     = dollar_match.group("mult") or ""
        value    = float(raw_val) * _parse_multiplier(mult)

        # Normalise display
        if value >= 1_000_000:
            normalised = f"${value / 1_000_000:.1f}M"
        elif value >= 1_000:
            normalised = f"${value:,.0f}"
        else:
            normalised = f"${value:.0f}"
        if is_upto:
            normalised = f"Up to {normalised}"
        if is_peryear:
            normalised = f"{normalised}/year"

        # Sanity checks
        is_per_unit = bool(_PER_UNIT_RE.search(text))
        flag, reason = False, None
        if value == 0:
            flag, reason = True, "amount is $0"
        elif value < _MIN_REASONABLE_DOLLAR and not is_per_unit:
            # Small values are only suspicious when they're total amounts, not per-unit rates
            flag, reason = True, f"suspiciously low amount: ${value}"
        elif value > _MAX_REASONABLE_DOLLAR:
            flag, reason = True, f"suspiciously high amount: ${value:,.0f} — verify"

        return AmountValidationResult(
            original=raw_amount,
            normalised=normalised,
            numeric_value=value,
            is_percentage=False,
            is_per_year=is_peryear,
            is_up_to=is_upto,
            flag=flag,
            flag_reason=reason,
        )

    # ── Try percentage ──────────────────────────────────────────────────────
    pct_match = _PCT_RE.search(text)
    if pct_match:
        value = float(pct_match.group("val"))
        normalised = f"{value:.0f}%" if value == int(value) else f"{value}%"
        if is_upto:
            normalised = f"Up to {normalised}"
        if is_peryear:
            normalised = f"{normalised}/year"

        flag, reason = False, None
        if value == 0:
            flag, reason = True, "percentage is 0%"
        elif value > 100:
            flag, reason = True, f"percentage > 100% ({value}%) — verify"

        return AmountValidationResult(
            original=raw_amount,
            normalised=normalised,
            numeric_value=value,
            is_percentage=True,
            is_per_year=is_peryear,
            is_up_to=is_upto,
            flag=flag,
            flag_reason=reason,
        )

    # ── No number found — still keep text but flag it ───────────────────────
    logger.debug("Amount has no parseable number: %r", text)
    return AmountValidationResult(
        original=raw_amount,
        normalised=text,        # keep as-is
        numeric_value=None,
        is_percentage=False,
        is_per_year=is_peryear,
        is_up_to=is_upto,
        flag=True,
        flag_reason="no numeric value found in amount string",
    )


def apply_amount_validation(record: dict) -> dict:
    """
    Run amount validation on one record dict (post-LLM or post-table).
    Normalises incentive_amount in-place and sets review_needed="Yes" if flagged.
    Returns the (possibly modified) record.
    """
    result = validate_amount(record.get("incentive_amount"))

    # Normalise the amount string
    if result.normalised and result.normalised != record.get("incentive_amount"):
        logger.debug(
            "Amount normalised for '%s': %r → %r",
            record.get("program_name"), record.get("incentive_amount"), result.normalised,
        )
        record["incentive_amount"] = result.normalised

    # Flag for human review if sanity check failed
    if result.flag:
        record["review_needed"] = "Yes"
        logger.info(
            "Amount flagged for '%s': %s",
            record.get("program_name", "?"), result.flag_reason,
        )

    return record


def apply_amount_validation_batch(records: list[dict]) -> list[dict]:
    """Apply amount validation to every record in the list."""
    return [apply_amount_validation(r) for r in records]
