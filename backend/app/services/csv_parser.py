"""CSV parsing utilities for lead upload.

Stays free of FastAPI/SQLAlchemy imports so it's trivially testable.
"""
from __future__ import annotations

import csv
import re
from io import StringIO

# Lead column → set of accepted header aliases (normalized at runtime).
COMMON_ALIASES: dict[str, set[str]] = {
    "email": {
        "email", "email_address", "work_email", "business_email",
        "email_id", "e_mail", "primary_email", "contact_email",
    },
    "first_name": {
        "first_name", "firstname", "first", "given_name", "fname",
    },
    "last_name": {
        "last_name", "lastname", "last", "surname", "family_name", "lname",
    },
    "company": {
        "company", "company_name", "organization", "org", "employer", "account",
    },
    "job_title": {
        "title", "job_title", "position", "role", "jobtitle",
    },
    "linkedin_url": {
        "linkedin", "linkedin_url", "linkedin_profile", "linkedinurl",
        "li_url", "linkedin_link",
    },
    "phone": {
        "phone", "phone_number", "mobile", "cell", "telephone", "mobile_number",
    },
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_csv_content(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Decode CSV bytes (utf-8-sig → utf-8 → latin-1 fallback) and return
    (column headers, list of row dicts).

    Empty cells decode as empty strings.  Rows shorter than the header are
    padded; longer rows have their extras dropped to keep the schema clean.
    """
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Could not decode CSV content with utf-8 or latin-1")

    reader = csv.DictReader(StringIO(text))
    columns = list(reader.fieldnames or [])
    rows: list[dict[str, str]] = []
    for row in reader:
        # Drop None keys (rows longer than the header) and coerce None values
        # to empty string.
        cleaned = {k: (v if v is not None else "") for k, v in row.items() if k is not None}
        rows.append(cleaned)
    return columns, rows


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Best-effort header→lead-field guesses. Each lead field is matched at
    most once (first column wins) to avoid two CSV columns competing for the
    same destination."""
    alias_lookup: dict[str, str] = {}
    for field, aliases in COMMON_ALIASES.items():
        for alias in aliases:
            alias_lookup[_normalize(alias)] = field

    result: dict[str, str] = {}
    used: set[str] = set()
    for col in columns:
        field = alias_lookup.get(_normalize(col))
        if field is not None and field not in used:
            result[col] = field
            used.add(field)
    return result


def select_sample_indices(total: int, sample_count: int) -> list[int]:
    """Pick indices spread evenly across [0, total-1], inclusive of both ends.

    For sample_count >= total returns every index. For sample_count == 1
    returns just [0]. Otherwise indices are distributed via linear interpolation
    and de-duplicated, so the returned list may be shorter than sample_count if
    rounding collisions occur on small inputs.
    """
    if total <= 0 or sample_count <= 0:
        return []
    if sample_count >= total:
        return list(range(total))
    if sample_count == 1:
        return [0]
    step = (total - 1) / (sample_count - 1)
    return sorted({int(round(i * step)) for i in range(sample_count)})
