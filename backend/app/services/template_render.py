"""Merge-field rendering for fully-templated campaigns.

A template is plain text with ``{{field}}`` placeholders.  Each placeholder
may carry an inline default after a pipe: ``{{first_name|there}}`` renders
"there" when the lead has no first name.

Field resolution (case-insensitive) looks first at the lead's standard
mapped columns, then at any raw CSV column header from the uploaded file.
A placeholder that resolves to nothing and has no inline default renders as
an empty string; runs of 2+ spaces left behind are collapsed so
``{{title}} {{name}}`` with an empty title doesn't leave a double space.

No AI, no external calls — this is the zero-cost path.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Lead

# Standard lead columns a template may reference by their snake_case name.
_STANDARD_FIELDS = (
    "first_name",
    "last_name",
    "company",
    "company_website",
    "job_title",
    "email",
    "phone",
    "linkedin_url",
)

# {{ key }} or {{ key | default }} — key has no '}' or '|'; default runs to '}}'.
_PLACEHOLDER = re.compile(r"\{\{\s*([^}|]+?)\s*(?:\|\s*(.*?)\s*)?\}\}", re.DOTALL)


def build_merge_context(lead: "Lead") -> dict[str, str]:
    """Flatten a lead into a {field_name: value} dict for rendering.

    Standard fields win over raw CSV columns of the same name; remaining raw
    columns are added so templates can reference arbitrary uploaded headers.
    Returns plain strings so the result is safe to use after the DB session
    that loaded ``lead`` has closed.
    """
    ctx: dict[str, str] = {}
    for field in _STANDARD_FIELDS:
        value = getattr(lead, field, None)
        ctx[field] = "" if value is None else str(value)

    for column, value in (lead.raw_csv_row or {}).items():
        # Don't let a raw column clobber a populated standard field, but do
        # let it fill one that's empty (e.g. an unmapped "Company" header).
        key = str(column)
        rendered = "" if value is None else str(value)
        if not ctx.get(key):
            ctx[key] = rendered

    return ctx


def render_template(text: str | None, ctx: dict[str, str]) -> str:
    """Substitute ``{{field}}`` / ``{{field|default}}`` placeholders.

    Lookup is case-insensitive.  Missing value + no default → empty string.
    """
    if not text:
        return ""

    lower_ctx = {k.lower(): v for k, v in ctx.items()}

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).strip().lower()
        default = match.group(2)
        value = lower_ctx.get(key, "")
        if not value and default is not None:
            return default
        return value

    rendered = _PLACEHOLDER.sub(_sub, text)
    # Collapse the gaps an empty placeholder leaves behind, without touching
    # newlines (so paragraph spacing survives).
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    return rendered
