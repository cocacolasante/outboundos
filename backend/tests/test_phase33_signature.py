"""Phase 33: signature apply service (pure function)."""
from app.services.signature import apply_signature

SIG = "Anthony Colasante\n555-123-4567\nacme.com\ncal.com/anthony"


def test_replaces_multiline_signoff():
    body = "Hi Jane,\n\nLove what Acme does.\n\nBest,\nAnthony"
    out = apply_signature(body, SIG)
    assert out == "Hi Jane,\n\nLove what Acme does.\n\n" + SIG
    assert "Best,\nAnthony" not in out  # old sign-off gone


def test_replaces_various_closings():
    for closing in ["Best regards,", "Regards,", "Thanks,", "Cheers,", "Sincerely,"]:
        body = f"Hi,\n\nBody here.\n\n{closing}\nAnthony"
        out = apply_signature(body, SIG)
        assert out.endswith(SIG)
        assert closing not in out


def test_appends_when_no_signoff_detected():
    body = "Hi Jane,\n\nQuick question about Acme. Let me know."
    out = apply_signature(body, SIG)
    assert out == body + "\n\n" + SIG


def test_idempotent_when_already_applied():
    body = "Hi,\n\nBody.\n\nBest,\nAnthony"
    once = apply_signature(body, SIG)
    twice = apply_signature(once, SIG)
    assert once == twice  # second apply is a no-op


def test_empty_signature_returns_body_unchanged():
    body = "Hi,\n\nBody.\n\nBest,\nAnthony"
    assert apply_signature(body, "") == body.rstrip()
    assert apply_signature(body, None) == body.rstrip()
    assert apply_signature(body, "   ") == body.rstrip()


def test_empty_body_returns_signature():
    assert apply_signature("", SIG) == SIG
    assert apply_signature(None, SIG) == SIG


def test_does_not_match_closing_word_mid_body():
    # "Thanks" deep in the body (not near the end) must NOT be treated as the
    # sign-off — only a closing line near the bottom is replaced.
    body = (
        "Hi Jane,\n\nThanks for the great talk last week.\n\n"
        "I wanted to follow up on a few things about Acme.\n\n"
        "Hope to connect soon and learn more about your roadmap.\n\n"
        "Best,\nAnthony"
    )
    out = apply_signature(body, SIG)
    assert "Thanks for the great talk" in out   # mid-body line preserved
    assert out.endswith(SIG)


# ---- strip_signoff ---------------------------------------------------------

def test_strip_signoff_removes_simple_closing():
    from app.services.signature import strip_signoff
    body = "Hey Jane,\n\nQuick thought after your Series B.\n\nBest,\nAnthony"
    out = strip_signoff(body)
    assert "Quick thought" in out
    assert "Best" not in out
    assert "Anthony" not in out


def test_strip_signoff_preserves_body_when_no_closing():
    from app.services.signature import strip_signoff
    body = "Hey Jane,\n\nQuick thought, what do you think?"
    assert strip_signoff(body) == body.rstrip()


def test_strip_signoff_handles_empty():
    from app.services.signature import strip_signoff
    assert strip_signoff(None) == ""
    assert strip_signoff("") == ""
    assert strip_signoff("   \n\n  ") == ""


def test_strip_signoff_does_not_strip_mid_body_thanks():
    """Anti-regression: 'thanks' mid-body shouldn't be treated as the
    sign-off and have everything after it stripped."""
    from app.services.signature import strip_signoff
    body = (
        "Hey Jane,\n\n"
        "Thanks for sharing the deck.  My thought after reading it: "
        "the founder story leads better than the TAM slide."
    )
    out = strip_signoff(body)
    # Whole body preserved — no sign-off near the end.
    assert "TAM slide" in out


# ---- signature_to_html ----------------------------------------------------

def test_signature_to_html_converts_naked_newlines_to_br():
    from app.services.signature import signature_to_html
    sig = "Best,\nAnthony\ncsuitecode.com"
    out = signature_to_html(sig)
    # Three lines → two <br> separators.
    assert out.count("<br>") == 2
    assert "Best," in out
    assert "csuitecode.com" in out


def test_signature_to_html_preserves_anchor_tags():
    from app.services.signature import signature_to_html
    sig = 'Best,\n<a href="https://csuitecode.com">csuitecode.com</a>'
    out = signature_to_html(sig)
    # The <a> tag passes through (now with auto-injected blue+underline
    # style — see test_signature_to_html_auto_styles_bare_anchor below
    # for the dedicated assertion on that).
    assert 'href="https://csuitecode.com"' in out
    assert ">csuitecode.com</a>" in out
    # And the newline before it became a <br>.
    assert "Best,<br>" in out


def test_signature_to_html_preserves_img_tags():
    from app.services.signature import signature_to_html
    sig = '<img src="https://example.com/logo.png" alt="Logo" style="max-width:120px;">\nAnthony'
    out = signature_to_html(sig)
    assert '<img src="https://example.com/logo.png"' in out
    assert 'style="max-width:120px;"' in out
    assert "<br>" in out  # newline after the img became <br>


def test_signature_to_html_returns_empty_for_blank_signature():
    from app.services.signature import signature_to_html
    assert signature_to_html(None) == ""
    assert signature_to_html("") == ""
    assert signature_to_html("   ") == ""


def test_signature_to_html_does_not_escape_existing_html():
    """Anti-regression: signature with `<strong>` etc. should reach the
    recipient as real bold text, not literal angle-brackets."""
    from app.services.signature import signature_to_html
    sig = "<strong>Anthony Colasante</strong>"
    out = signature_to_html(sig)
    assert "<strong>" in out
    assert "&lt;" not in out


def test_signature_to_html_auto_styles_bare_anchor_blue_underline():
    """A bare <a href=…>text</a> gets the default blue+underline style
    inline so it renders consistently across clients (Gmail and some
    mobile email clients strip user-agent default <a> styling)."""
    from app.services.signature import signature_to_html
    out = signature_to_html('Best,\n<a href="https://csuitecode.com">csuitecode.com</a>')
    assert 'style="color:#1d4ed8;text-decoration:underline;"' in out
    # And the href / text are still there.
    assert 'href="https://csuitecode.com"' in out
    assert '>csuitecode.com</a>' in out


def test_signature_to_html_leaves_user_styled_anchor_alone():
    """If the user wrote their own ``style=`` on the anchor (e.g. black
    or a custom brand color), respect that — don't clobber with default."""
    from app.services.signature import signature_to_html
    sig = '<a href="https://csuitecode.com" style="color:#000;">csuitecode.com</a>'
    out = signature_to_html(sig)
    # User's color preserved, default NOT injected on top.
    assert 'style="color:#000;"' in out
    assert "#1d4ed8" not in out


def test_signature_to_html_styles_multiple_anchors_independently():
    from app.services.signature import signature_to_html
    sig = (
        '<a href="https://csuitecode.com">site</a> · '
        '<a href="https://calendly.com/me" style="color:#000;">book</a>'
    )
    out = signature_to_html(sig)
    # First anchor gets the default style; second keeps the user's.
    assert out.count("#1d4ed8") == 1
    assert "color:#000;" in out


def test_signature_to_html_inject_is_idempotent():
    """Running the renderer twice (e.g. preview then send) doesn't
    duplicate the inline style attribute."""
    from app.services.signature import signature_to_html
    sig = '<a href="https://csuitecode.com">x</a>'
    once = signature_to_html(sig)
    twice = signature_to_html(once)
    assert once == twice
    assert twice.count("style=") == 1


# ---- signature_to_text ----------------------------------------------------

def test_signature_to_text_links_collapse_to_text_paren_url():
    from app.services.signature import signature_to_text
    sig = (
        "Best,\nAnthony\n"
        '<a href="https://csuitecode.com">csuitecode.com</a> · '
        '<a href="https://calendly.com/anthony">book a call</a>'
    )
    out = signature_to_text(sig)
    assert "csuitecode.com (https://csuitecode.com)" in out
    assert "book a call (https://calendly.com/anthony)" in out
    # No angle brackets left.
    assert "<" not in out
    assert ">" not in out


def test_signature_to_text_image_to_bracketed_alt():
    from app.services.signature import signature_to_text
    sig = '<img src="https://example.com/logo.png" alt="Logo">'
    out = signature_to_text(sig)
    assert "[image: Logo — https://example.com/logo.png]" == out


def test_signature_to_text_image_without_alt_falls_back_to_url_only():
    from app.services.signature import signature_to_text
    sig = '<img src="https://example.com/logo.png">'
    out = signature_to_text(sig)
    assert "[image: https://example.com/logo.png]" == out


def test_signature_to_text_br_becomes_newline():
    from app.services.signature import signature_to_text
    sig = "Anthony<br>csuitecode.com<br/>415-555-0100"
    out = signature_to_text(sig)
    # Both <br> variants → newlines.
    lines = out.split("\n")
    assert "Anthony" in lines
    assert "csuitecode.com" in lines
    assert "415-555-0100" in lines


def test_signature_to_text_strips_unknown_tags_keeps_inner_text():
    from app.services.signature import signature_to_text
    sig = "<strong>Anthony</strong> <em>Colasante</em>"
    out = signature_to_text(sig)
    assert out == "Anthony Colasante"


def test_signature_to_text_decodes_common_entities():
    from app.services.signature import signature_to_text
    sig = "Smith &amp; Jones &mdash; cofounder"
    out = signature_to_text(sig)
    assert "Smith & Jones" in out


def test_signature_to_text_empty_signature_returns_empty():
    from app.services.signature import signature_to_text
    assert signature_to_text(None) == ""
    assert signature_to_text("") == ""


# ---- Review-fix regressions -------------------------------------------------

def test_signature_to_html_escapes_plain_text_angle_brackets():
    """Plain-text angle-bracket content (NOT an allowlisted tag) must be
    HTML-escaped so mail clients display it instead of silently dropping
    a fake tag.  Regression: '<Acme & Co>' used to pass through verbatim
    and vanish in the recipient's client."""
    from app.services.signature import signature_to_html
    out = signature_to_html("CEO <Acme & Co>\na < b")
    assert "&lt;Acme &amp; Co&gt;" in out
    assert "a &lt; b" in out
    # And no un-escaped fake tag survived.
    assert "<Acme" not in out


def test_signature_to_html_default_style_not_fooled_by_style_in_href():
    """'style=' inside a quoted href value must NOT suppress the default
    link styling — only a real style attribute does."""
    from app.services.signature import signature_to_html
    out = signature_to_html(
        '<a href="https://x.com/book?style=compact">book</a>'
    )
    assert "color:#1d4ed8" in out  # default injected despite ?style= in URL


def test_signature_to_html_styles_bare_anchor_without_attributes():
    from app.services.signature import signature_to_html
    out = signature_to_html("<a>click</a>")
    assert 'style="color:#1d4ed8;text-decoration:underline;"' in out


def test_signature_to_text_does_not_double_decode_nested_entities():
    """'&amp;lt;' means the user wants the literal text '&lt;' displayed.
    The old hand-rolled decode loop replaced '&amp;' FIRST, producing
    '&lt;' then '<' — html.unescape decodes correctly in one pass."""
    from app.services.signature import signature_to_text
    out = signature_to_text("tips &amp;lt;3 from us")
    assert "&lt;3" in out
    assert "<3" not in out


def test_render_email_with_signature_idempotent_on_raw_signature_tail():
    """A body that already ends with the raw signature (apply_signature
    merged it at compose time) must not get it appended twice."""
    from app.services.signature import render_email_with_signature
    sig = "Best,\nAnthony\ncsuitecode.com"
    body = "Hi Jane.\n\nQuick thought.\n\n" + sig
    html_body, text_body = render_email_with_signature(body, sig)
    # The signature contact line appears exactly once in the text body.
    assert text_body.count("csuitecode.com") == 1
    assert html_body.count("csuitecode.com") == 1


def test_render_email_with_signature_idempotent_on_text_form_tail():
    """A body ending with the signature's PLAIN-TEXT form (e.g. the user
    pasted a previously-sent email whose text body carried the rendered
    signature) is also recognised and not doubled."""
    from app.services.signature import render_email_with_signature, signature_to_text
    sig = '<a href="https://csuitecode.com">csuitecode.com</a>\nAnthony'
    sig_text = signature_to_text(sig)
    body = "Hi Jane.\n\nQuick thought.\n\n" + sig_text
    html_body, text_body = render_email_with_signature(body, sig)
    assert text_body.count("csuitecode.com (https://csuitecode.com)") == 1
    # HTML body carries the real anchor exactly once.
    assert html_body.count('href="https://csuitecode.com"') == 1


def test_render_email_with_signature_no_signature_plain_render():
    from app.services.signature import render_email_with_signature
    html_body, text_body = render_email_with_signature("Hi.\n\nBye.", None)
    assert "Hi." in html_body and "Bye." in html_body
    assert text_body == "Hi.\n\nBye."
    assert "signature" not in html_body  # no signature div injected


def test_render_email_with_signature_strips_ai_signoff():
    """No signature in the body yet + an AI sign-off at the tail → the
    sign-off is replaced by the signature (not stacked above it)."""
    from app.services.signature import render_email_with_signature
    sig = "—\nAnthony · csuitecode.com"
    body = "Hi Jane.\n\nWorth a chat?\n\nBest,\nAnthony"
    html_body, text_body = render_email_with_signature(body, sig)
    # Sign-off line gone; signature present once.
    assert "Best," not in text_body
    assert text_body.count("csuitecode.com") == 1
