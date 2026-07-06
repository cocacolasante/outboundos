"""Phase 32: merge-field template rendering (pure functions, no DB)."""
from types import SimpleNamespace

from app.services.template_render import build_merge_context, render_template


def _lead(**kw):
    base = {
        "first_name": None,
        "last_name": None,
        "company": None,
        "company_website": None,
        "job_title": None,
        "email": None,
        "phone": None,
        "linkedin_url": None,
        "raw_csv_row": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- build_merge_context ----------


def test_context_has_standard_fields_as_strings():
    ctx = build_merge_context(_lead(first_name="Jane", company="Acme"))
    assert ctx["first_name"] == "Jane"
    assert ctx["company"] == "Acme"
    # Unset standard fields are empty strings, not None.
    assert ctx["last_name"] == ""


def test_context_includes_raw_csv_columns():
    ctx = build_merge_context(_lead(raw_csv_row={"Industry": "SaaS", "Notes": "VIP"}))
    assert ctx["Industry"] == "SaaS"
    assert ctx["Notes"] == "VIP"


def test_raw_column_fills_empty_standard_but_not_populated_one():
    lead = _lead(company="Acme", raw_csv_row={"company": "ShouldNotWin", "city": "NYC"})
    ctx = build_merge_context(lead)
    assert ctx["company"] == "Acme"  # populated standard field wins
    assert ctx["city"] == "NYC"


# ---------- render_template ----------


def test_basic_substitution():
    ctx = build_merge_context(_lead(first_name="Sarah", company="Acme"))
    assert render_template("Hi {{first_name}} at {{company}}", ctx) == "Hi Sarah at Acme"


def test_inline_default_used_when_value_missing():
    ctx = build_merge_context(_lead())
    assert render_template("Hi {{first_name|there}},", ctx) == "Hi there,"


def test_inline_default_ignored_when_value_present():
    ctx = build_merge_context(_lead(first_name="Sarah"))
    assert render_template("Hi {{first_name|there}},", ctx) == "Hi Sarah,"


def test_missing_field_no_default_collapses_spaces():
    ctx = build_merge_context(_lead(first_name="Sarah"))
    # {{title}} is empty → the double space it leaves is collapsed.
    assert render_template("{{title}} {{first_name}}", ctx) == " Sarah"


def test_case_insensitive_lookup():
    ctx = build_merge_context(_lead(first_name="Sarah", raw_csv_row={"Industry": "SaaS"}))
    assert render_template("{{First_Name}} / {{industry}}", ctx) == "Sarah / SaaS"


def test_whitespace_inside_braces_is_tolerated():
    ctx = build_merge_context(_lead(company="Acme"))
    assert render_template("{{  company  }}", ctx) == "Acme"


def test_newlines_preserved():
    ctx = build_merge_context(_lead(first_name="Sarah"))
    out = render_template("Hi {{first_name}},\n\nThanks.", ctx)
    assert out == "Hi Sarah,\n\nThanks."


def test_none_or_empty_template_renders_empty():
    assert render_template(None, {}) == ""
    assert render_template("", {}) == ""
