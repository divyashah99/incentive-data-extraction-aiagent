"""
Text Sanitizer
==============
Strips URLs and link references from free-text fields (`description`,
`eligibility_criteria`) so they're safe to render in the Dreamline dashboard
UI without sending homeowners off to external application pages.

Application URLs are NOT lost — they remain in the dedicated `program_links`
field, which the dashboard renders as an opt-in call-to-action.

Handles:
  • Bare URLs:        https://x.com/path  |  www.x.com
  • Markdown:         [Apply here](https://x.com)   → "Apply here"
  • HTML anchors:     <a href="...">Apply</a>       → "Apply"
  • Parenthetical:    "(https://x.com)"             → ""
  • Sentences whose entire purpose is a link reference, e.g.
    "For more info, visit https://x.com." → dropped entirely
  • Stand-alone URL sentences:   "https://x.com" → dropped entirely
  • Incidental URLs mid-sentence: URL is removed; surrounding text is preserved
"""
import logging
import re

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────────

# Markdown link [text](url) — preserve the visible text
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*(?:https?://|www\.)[^\)]+\)", re.IGNORECASE)

# HTML <a ...>text</a> — preserve the visible text
_HTML_LINK_RE = re.compile(r'<a\s[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

# URL pattern — permissive within the URL, strict about what ends it.
# `[^\s<>"'),\]]+` lets dots inside the URL (example.com) but stops at whitespace
# or punctuation that's clearly outside the URL.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'),\]]+",
    re.IGNORECASE,
)

# Phrases that introduce a link — when one of these appears in a sentence
# alongside a URL, the WHOLE sentence is a link reference and should be dropped.
_LINK_INTRO_RE = re.compile(
    r"""
    \b(?:
        visit
      | see
      | apply\s+(?:at|here|online|via)
      | more\s+info(?:rmation)?(?:\s+at|\s+on)?
      | (?:for\s+)?(?:more\s+)?(?:info(?:rmation)?|details)(?:\s+at|\s+on)?
      | learn\s+more(?:\s+at|\s+on|\s+by\s+visiting)?
      | click\s+(?:here|the\s+link)
      | go\s+to
      | available\s+(?:at|on|online\s+at)
      | check\s+(?:out|at)
      | website[:]?
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Parenthetical containing only a placeholder, e.g. "(URLPLACEHOLDER)"
# (handled after URLs are replaced with placeholders)

# Cleanup patterns
_MULTISPACE_RE          = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE  = re.compile(r"\s+([,.;:!?])")
_DOUBLE_PUNCT_RE        = re.compile(r"([.!?;:])\s*\1+")
_EMPTY_PAREN_RE         = re.compile(r"\(\s*\)")
_TRAILING_CONJUNCTION_RE = re.compile(r"\s+(?:and|or|but|with|via|through|on|at|by|to)\s*[.!?,;:]?\s*$", re.IGNORECASE)

_URL_PLACEHOLDER = "\x00URL\x00"
# Split on sentence boundaries, keeping the delimiter so we can rejoin cleanly.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ── Public API ────────────────────────────────────────────────────────────────

def strip_links(text: str | None) -> str | None:
    """
    Remove URLs and link references from `text` while preserving narrative content.
    Returns the cleaned string, or None if the result is empty.
    """
    if not text:
        return text

    original = text

    # 1. Markdown [text](url) → text
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)

    # 2. HTML <a>text</a> → text
    text = _HTML_LINK_RE.sub(r"\1", text)

    # 3. Replace every remaining URL with a placeholder so we can reason about
    #    sentences without URL internal-dot noise.
    text = _URL_RE.sub(_URL_PLACEHOLDER, text)

    # 4. Strip parenthetical wrappers around a placeholder: "( URLPLACEHOLDER )"
    text = re.sub(
        r"\s*\(\s*" + re.escape(_URL_PLACEHOLDER) + r"\s*\)",
        "",
        text,
    )

    # 5. Sentence-level pass.
    sentences = _SENTENCE_SPLIT_RE.split(text)
    cleaned: list[str] = []
    for sent in sentences:
        stripped = sent.strip()
        if not stripped:
            continue

        has_placeholder = _URL_PLACEHOLDER in stripped
        has_link_intro  = bool(_LINK_INTRO_RE.search(stripped))

        # Drop sentences whose purpose is a link reference
        if has_placeholder and has_link_intro:
            continue

        # Drop sentences that are URL-only (placeholder + optional punctuation)
        if has_placeholder and re.fullmatch(
            r"[\s,.;:!?\-—–]*" + re.escape(_URL_PLACEHOLDER) + r"[\s,.;:!?\-—–]*",
            stripped,
        ):
            continue

        # Otherwise: the URL was incidental — drop just the placeholder, keep the text.
        if has_placeholder:
            stripped = stripped.replace(_URL_PLACEHOLDER, "")

        # Tidy: remove trailing dangling connectives like "Apply at  or call..." → "...or call..."
        # We do this only when a placeholder was stripped from this sentence.
        stripped = _MULTISPACE_RE.sub(" ", stripped).strip()
        cleaned.append(stripped)

    text = " ".join(cleaned)

    # 6. Final whitespace / punctuation cleanup
    text = _MULTISPACE_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _DOUBLE_PUNCT_RE.sub(r"\1", text)
    text = _EMPTY_PAREN_RE.sub("", text)
    text = _TRAILING_CONJUNCTION_RE.sub("", text)
    text = text.strip(" \t\n,;:.-")

    if text != original:
        logger.debug("strip_links cleaned link content from text")

    return text or None


def sanitize_record(record: dict) -> dict:
    """Strip links from description and eligibility_criteria in-place."""
    for field in ("description", "eligibility_criteria"):
        value = record.get(field)
        if value:
            cleaned = strip_links(value)
            if cleaned != value:
                record[field] = cleaned
    return record


def sanitize_batch(records: list[dict]) -> list[dict]:
    """Apply link sanitisation to every record. Returns the mutated list."""
    touched = 0
    for r in records:
        before = (r.get("description") or "") + (r.get("eligibility_criteria") or "")
        sanitize_record(r)
        after = (r.get("description") or "") + (r.get("eligibility_criteria") or "")
        if before != after:
            touched += 1
    if touched:
        logger.info("Text sanitizer stripped links from %d record(s)", touched)
    return records
