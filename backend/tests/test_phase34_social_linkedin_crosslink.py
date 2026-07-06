"""Unit tests for ``services/social_listening_linkedin_crosslink.py``.

Covers the URL regex, canonicaliser, slug/URN extractors, and the
synthetic-opportunity builder.  No DB, no Anthropic, no HTTP — these
are pure-function tests.
"""
from __future__ import annotations

from app.models import SocialOpportunityAction, SocialOpportunityCategory
from app.services import social_listening_linkedin_crosslink as crosslink


# ---------------------------------------------------------------------------
# URL regex
# ---------------------------------------------------------------------------

def test_extract_matches_posts_pattern():
    text = (
        "Saw this rant earlier today: "
        "https://www.linkedin.com/posts/jane-doe-abc123_msp-frustration-activity-7284763839472640000-x_2A "
        "— exactly what we're talking about."
    )
    urls = crosslink.extract_linkedin_urls(text)
    assert len(urls) == 1
    assert urls[0].startswith("https://www.linkedin.com/posts/jane-doe-abc123_")


def test_extract_matches_feed_update_pattern():
    text = (
        "Original: https://www.linkedin.com/feed/update/urn:li:activity:7284763839472640000/ "
        "if anyone's curious."
    )
    urls = crosslink.extract_linkedin_urls(text)
    assert len(urls) == 1
    assert "urn:li:activity:7284763839472640000" in urls[0]


def test_extract_ignores_profile_urls():
    """``/in/<slug>`` is a profile, not a post — must NOT match."""
    text = "Connect with me: https://www.linkedin.com/in/john-doe/"
    assert crosslink.extract_linkedin_urls(text) == []


def test_extract_ignores_company_urls():
    text = "Acme is hiring: https://www.linkedin.com/company/acme-inc/about/"
    assert crosslink.extract_linkedin_urls(text) == []


def test_extract_strips_trailing_punctuation():
    """Markdown link closers and sentence punctuation shouldn't end up in the URL."""
    text = (
        "Worth a read: (https://www.linkedin.com/posts/jane_msp-rant-activity-12345_x). "
        "Also relevant — https://www.linkedin.com/feed/update/urn:li:activity:99999, agreed?"
    )
    urls = crosslink.extract_linkedin_urls(text)
    assert len(urls) == 2
    for u in urls:
        assert not u.endswith(")")
        assert not u.endswith(",")
        assert not u.endswith(".")


def test_extract_dedupes_within_one_post():
    """Same URL written twice yields one entry; order preserved by first occurrence."""
    text = (
        "First mention: https://www.linkedin.com/posts/jane_x_activity-1\n"
        "And again: https://www.linkedin.com/posts/jane_x_activity-1\n"
        "Different post: https://www.linkedin.com/posts/bob_y_activity-2"
    )
    urls = crosslink.extract_linkedin_urls(text)
    assert len(urls) == 2
    assert "activity-1" in urls[0]
    assert "activity-2" in urls[1]


def test_extract_empty_string():
    assert crosslink.extract_linkedin_urls("") == []
    assert crosslink.extract_linkedin_urls(None) == []  # type: ignore[arg-type]


def test_extract_accepts_m_subdomain():
    """LinkedIn's mobile host is ``m.linkedin.com`` — also valid."""
    text = "On phone: https://m.linkedin.com/posts/jane_x_activity-1"
    urls = crosslink.extract_linkedin_urls(text)
    assert len(urls) == 1
    assert "m.linkedin.com" in urls[0]


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------

def test_canonical_drops_query_and_fragment():
    raw = "https://www.linkedin.com/posts/jane_x_activity-1?utm_source=share#comment"
    assert crosslink.canonical_url(raw) == "https://www.linkedin.com/posts/jane_x_activity-1"


def test_canonical_lowercases_host():
    raw = "https://WWW.LinkedIn.COM/posts/JaneDoe_X_activity-1"
    out = crosslink.canonical_url(raw)
    assert out.startswith("https://www.linkedin.com/")
    # Path case preserved (slug case can matter on some surfaces).
    assert "JaneDoe_X_activity-1" in out


def test_canonical_strips_trailing_slash():
    raw = "https://www.linkedin.com/posts/jane_x_activity-1/"
    assert crosslink.canonical_url(raw) == "https://www.linkedin.com/posts/jane_x_activity-1"


def test_canonical_dedupe_via_query_strip():
    """Two flavours of the same post URL — with and without tracking params —
    must canonicalize to the SAME string so the existing UNIQUE constraint
    dedupes them."""
    a = "https://www.linkedin.com/posts/jane_x_activity-1?utm_source=share&trk=public_post"
    b = "https://www.linkedin.com/posts/jane_x_activity-1"
    assert crosslink.canonical_url(a) == crosslink.canonical_url(b)


def test_canonical_blank_returns_empty():
    assert crosslink.canonical_url("") == ""
    assert crosslink.canonical_url("   ") == ""
    # Not a URL at all.
    assert crosslink.canonical_url("just text") == ""


# ---------------------------------------------------------------------------
# extract_author_slug
# ---------------------------------------------------------------------------

def test_extract_author_slug_from_posts_url():
    url = "https://www.linkedin.com/posts/jane-doe-abc123_msp-rant-activity-12345"
    assert crosslink.extract_author_slug(url) == "jane-doe-abc123"


def test_extract_author_slug_none_for_feed_update():
    url = "https://www.linkedin.com/feed/update/urn:li:activity:12345"
    assert crosslink.extract_author_slug(url) is None


def test_extract_author_slug_none_for_non_post_url():
    assert crosslink.extract_author_slug("https://example.com") is None
    assert crosslink.extract_author_slug("") is None
    assert crosslink.extract_author_slug(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_activity_urn
# ---------------------------------------------------------------------------

def test_extract_activity_urn_from_feed_update():
    url = "https://www.linkedin.com/feed/update/urn:li:activity:7284763839472640000/"
    assert crosslink.extract_activity_urn(url) == "urn:li:activity:7284763839472640000"


def test_extract_activity_urn_from_posts_slug():
    url = "https://www.linkedin.com/posts/jane_x_activity-7284763839472640000-x_2A"
    assert crosslink.extract_activity_urn(url) == "urn:li:activity:7284763839472640000"


def test_extract_activity_urn_none_when_absent():
    assert crosslink.extract_activity_urn("https://www.linkedin.com/in/jane") is None
    assert crosslink.extract_activity_urn("") is None


# ---------------------------------------------------------------------------
# build_synthetic_discovered_post
# ---------------------------------------------------------------------------

def test_build_synthetic_post_shape():
    li_url = "https://www.linkedin.com/posts/jane-doe-abc_msp-rant-activity-12345"
    post = crosslink.build_synthetic_discovered_post(
        linkedin_url=li_url,
        reddit_post_url="https://www.reddit.com/r/msp/comments/abc/thread/",
        reddit_excerpt="Someone on LinkedIn just posted about this exact thing.",
    )
    assert post.post_url == li_url
    assert post.provider_post_id == "urn:li:activity:12345"
    assert post.author_profile_url == "https://www.linkedin.com/in/jane-doe-abc"
    assert post.author_name is None
    assert post.post_date is None
    assert post.raw["source"] == "reddit_crosslink"
    assert post.raw["reddit_url"].endswith("/abc/thread/")


def test_build_synthetic_post_caps_excerpt_at_1000_chars():
    long_excerpt = "x" * 5000
    post = crosslink.build_synthetic_discovered_post(
        linkedin_url="https://www.linkedin.com/posts/jane_x_activity-1",
        reddit_post_url="https://www.reddit.com/r/x/comments/y/",
        reddit_excerpt=long_excerpt,
    )
    assert len(post.post_text) == 1000


def test_build_synthetic_post_handles_feed_update_url():
    """No author slug on /feed/update/ URLs — author_profile_url stays None."""
    li_url = "https://www.linkedin.com/feed/update/urn:li:activity:99999"
    post = crosslink.build_synthetic_discovered_post(
        linkedin_url=li_url,
        reddit_post_url="https://www.reddit.com/r/x/comments/y/",
        reddit_excerpt="",
    )
    assert post.author_profile_url is None
    assert post.provider_post_id == "urn:li:activity:99999"


# ---------------------------------------------------------------------------
# build_synthetic_qualification
# ---------------------------------------------------------------------------

def test_build_synthetic_qualification_shape():
    """Score 5, category general_advisory, action research_further,
    empty suggested copy — documented contract."""
    q = crosslink.build_synthetic_qualification(
        reddit_post_url="https://www.reddit.com/r/sysadmin/comments/abc/foo/",
    )
    assert q.score == 5
    assert q.category == SocialOpportunityCategory.GENERAL_ADVISORY.value
    assert q.buying_signal is False
    assert q.recommended_action == SocialOpportunityAction.RESEARCH_FURTHER.value
    assert q.suggested_comment == ""
    assert q.suggested_connection_request == ""
    assert q.suggested_follow_up == ""
    assert "Reddit" in q.pain_summary
    assert "reddit.com/r/sysadmin/comments/abc/foo/" in q.qualification_reason
