"""
Incentive-type normalization (spec v2.7 §4.2).

Single source of truth for mapping arbitrary incentive-type strings to the
five canonical values. Shared by the LLM parser, table parser, and schema
validator so a label can only enter the main CSV through one gate.
"""
from __future__ import annotations

VALID_INCENTIVE_TYPES: set[str] = {
    "Grants",
    "Rebates",
    "Finance Solutions",
    "Tax Credits",
    "Investments",
}

# Aliases observed in real output (LLM hallucinations, source wording).
# Keys are lowercase + stripped. Grow as new aliases appear.
ALIAS_MAP: dict[str, str] = {
    # Rebates
    "rebate": "Rebates",
    "rebates": "Rebates",
    "rebate program": "Rebates",
    "utility rebate program": "Rebates",
    "performance-based incentive": "Rebates",
    "bill credit": "Rebates",
    "bill credits": "Rebates",
    "cash back": "Rebates",
    "cashback": "Rebates",
    # Grants
    "grant": "Grants",
    "grants": "Grants",
    "grant program": "Grants",
    "green building incentive": "Grants",
    "industry recruitment/support": "Grants",
    "generation incentive program": "Grants",
    # Tax Credits
    "tax credit": "Tax Credits",
    "tax credits": "Tax Credits",
    "tax exemption": "Tax Credits",
    "tax deduction": "Tax Credits",
    "corporate tax credit": "Tax Credits",
    "personal tax credit": "Tax Credits",
    "corporate tax exemption": "Tax Credits",
    "personal tax exemption": "Tax Credits",
    "corporate tax deduction": "Tax Credits",
    "personal tax deduction": "Tax Credits",
    "corporate depreciation": "Tax Credits",
    "property tax exemption": "Tax Credits",
    "property tax incentive": "Tax Credits",
    "property tax assessment": "Tax Credits",
    "sales tax incentive": "Tax Credits",
    "sales tax exemption": "Tax Credits",
    "value-added tax exemption": "Tax Credits",
    # Finance Solutions
    "loan": "Finance Solutions",
    "loans": "Finance Solutions",
    "loan program": "Finance Solutions",
    "pace": "Finance Solutions",
    "pace financing": "Finance Solutions",
    "leasing": "Finance Solutions",
    "green power purchasing": "Finance Solutions",
    "energy savings performance contract": "Finance Solutions",
    "production incentive": "Finance Solutions",
    "financing": "Finance Solutions",
    "second mortgage": "Finance Solutions",
    "down payment assistance": "Finance Solutions",
    # Investments
    "bond program": "Investments",
    "revolving loan fund": "Investments",
    "investment": "Investments",
    "investments": "Investments",
}


def normalize_incentive_type(value: str | None) -> str | None:
    """
    Map an arbitrary incentive-type string to one of the five canonical values.

    Returns the canonical value when matched, else None. A None return is the
    signal to the caller to quarantine the row with reason
    ``unmapped incentive_type: <raw value>``.
    """
    if not value:
        return None
    key = value.strip().lower()
    if not key:
        return None
    for canonical in VALID_INCENTIVE_TYPES:
        if key == canonical.lower():
            return canonical
    return ALIAS_MAP.get(key)
