"""Phase 5: CSV parser, mapping suggestion, sample selection."""
import pytest

from app.services import csv_parser


def test_parses_utf8_csv():
    content = b"Email,First Name\nalice@x.com,Alice\nbob@y.com,Bob\n"
    cols, rows = csv_parser.parse_csv_content(content)
    assert cols == ["Email", "First Name"]
    assert rows == [
        {"Email": "alice@x.com", "First Name": "Alice"},
        {"Email": "bob@y.com", "First Name": "Bob"},
    ]


def test_strips_utf8_bom():
    content = "﻿Email,Name\na@x.com,A\n".encode("utf-8")
    cols, rows = csv_parser.parse_csv_content(content)
    assert cols == ["Email", "Name"]


def test_falls_back_to_latin1_on_invalid_utf8():
    # 0xff is invalid as utf-8 leading byte; valid in latin-1.
    content = b"Email,Name\na@x.com,Andr\xe9\n"
    cols, rows = csv_parser.parse_csv_content(content)
    assert cols == ["Email", "Name"]
    assert rows[0]["Name"] == "André"


def test_empty_csv_returns_empty_lists():
    cols, rows = csv_parser.parse_csv_content(b"")
    assert cols == []
    assert rows == []


# ---------- suggest_mapping ----------


def test_suggested_mapping_matches_common_headers():
    cols = ["Email Address", "First Name", "Last Name", "Company", "Job Title", "LinkedIn URL"]
    mapping = csv_parser.suggest_mapping(cols)
    assert mapping == {
        "Email Address": "email",
        "First Name": "first_name",
        "Last Name": "last_name",
        "Company": "company",
        "Job Title": "job_title",
        "LinkedIn URL": "linkedin_url",
    }


def test_suggested_mapping_ignores_unknown_columns():
    cols = ["Email", "Whatever Field", "Random"]
    mapping = csv_parser.suggest_mapping(cols)
    assert mapping == {"Email": "email"}


def test_suggested_mapping_handles_case_and_punctuation():
    cols = ["E-Mail", "FIRSTNAME", "linkedin_profile"]
    mapping = csv_parser.suggest_mapping(cols)
    assert mapping == {
        "E-Mail": "email",
        "FIRSTNAME": "first_name",
        "linkedin_profile": "linkedin_url",
    }


def test_each_field_assigned_at_most_once():
    cols = ["Email", "Email Address"]  # both match email
    mapping = csv_parser.suggest_mapping(cols)
    assert list(mapping.values()) == ["email"]  # only one wins


# ---------- select_sample_indices ----------


@pytest.mark.parametrize("total,n,expected", [
    # 10 leads, 5 samples: step = 9/4 = 2.25 → indices 0, 2.25, 4.5, 6.75, 9.
    # Python's banker's rounding takes 4.5 → 4.
    (10, 5, [0, 2, 4, 7, 9]),
    (10, 1, [0]),
    (10, 2, [0, 9]),
    (10, 10, list(range(10))),
    (5, 20, list(range(5))),  # cap at total
    (0, 5, []),
    (5, 0, []),
])
def test_sample_index_selection(total, n, expected):
    assert csv_parser.select_sample_indices(total, n) == expected


def test_sample_indices_always_include_endpoints():
    for total in (3, 7, 11, 50):
        for n in (2, 3, 4, 5):
            if n > total:
                continue
            idx = csv_parser.select_sample_indices(total, n)
            assert idx[0] == 0
            assert idx[-1] == total - 1
