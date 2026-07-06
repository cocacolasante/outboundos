"""Apply a campaign signature to a composed email body.

The AI composer ends each email with a sign-off (e.g. ``Best,\\nAnthony``)
that often lacks contact info / website / calendar link.  ``apply_signature``
swaps that sign-off for the campaign's signature block, falling back to a
plain append when no recognizable sign-off is found.  It is idempotent — a
body that already ends with the signature is returned unchanged — so the
bulk-apply endpoint and a re-compose can run safely more than once.

HTML signatures (links, images, basic formatting typed via the Settings
toolbar) are rendered by ``render_email_with_signature`` — the ONE shared
entry point both the research-client one-off send and the bulk campaign
send path use.  It:

  1. Strips the signature (raw or text form) off the body tail when it's
     already there (idempotency), else strips the AI sign-off line.
  2. Renders the stripped body through the standard
     ``render_html`` / ``render_text``.
  3. Appends the signature via ``signature_to_html`` (allowlisted tags
     pass through verbatim, plain text is HTML-escaped, naked newlines
     outside tags become ``<br>``) for the HTML body, and via
     ``signature_to_text`` (collapses tags into plain-text equivalents)
     for the text body.
"""
from __future__ import annotations

import html as _html
import re

# Common email closings the AI emits, lowercased, punctuation stripped.  A
# line that IS one of these (its own line, as Claude formats them) marks the
# start of the sign-off block we replace.
_CLOSINGS: frozenset[str] = frozenset({
    "best", "best regards", "warm regards", "warmest regards", "kind regards",
    "kindest regards", "regards", "thanks", "thank you", "many thanks",
    "thanks so much", "cheers", "sincerely", "warmly", "talk soon",
    "all the best", "with appreciation", "with gratitude", "respectfully",
    "looking forward", "speak soon", "yours", "yours truly", "best wishes",
})

# Don't scan further than this many lines up from the end — the sign-off is
# always near the bottom, and scanning the whole body risks matching a word
# like "Thanks" mid-message.
_MAX_SIGNOFF_LOOKBACK = 6


def _normalize_closing(line: str) -> str:
    return line.strip().rstrip(",.!:;").strip().lower()


def strip_signoff(body: str | None) -> str:
    """Return ``body`` with the AI's sign-off block stripped if one is
    detected near the end (a ``_CLOSINGS`` line within the last
    ``_MAX_SIGNOFF_LOOKBACK`` lines).  Empty/None body → empty string."""
    body = (body or "").rstrip()
    if not body:
        return ""
    lines = body.split("\n")
    cut: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if len(lines) - 1 - i > _MAX_SIGNOFF_LOOKBACK:
            break
        if _normalize_closing(lines[i]) in _CLOSINGS:
            cut = i
            break
    if cut is None:
        return body
    return "\n".join(lines[:cut]).rstrip()


def apply_signature(body: str | None, signature: str | None) -> str:
    """Return ``body`` with its sign-off replaced by ``signature``.

    - Empty signature → body unchanged.
    - Body already ending with the signature → unchanged (idempotent).
    - A recognizable closing line near the end → everything from it to the
      end is replaced with the signature.
    - Otherwise the signature is appended after a blank line.
    """
    body = (body or "").rstrip()
    sig = (signature or "").strip()
    if not sig:
        return body
    if not body:
        return sig
    if body.endswith(sig):
        return body  # idempotent — already applied

    kept = strip_signoff(body)
    return f"{kept}\n\n{sig}" if kept else sig


async def resolve_campaign_signature(session, campaign) -> str | None:
    """The signature actually applied to a campaign's emails.

    The campaign's own ``signature`` is a per-campaign OVERRIDE; when it's
    blank the campaign inherits the bound connected account's signature
    (the one configured in Settings).  Returns the effective signature, or
    None when neither is set.
    """
    if (getattr(campaign, "signature", None) or "").strip():
        return campaign.signature
    account_id = getattr(campaign, "connected_account_id", None)
    if account_id is not None:
        # Lazy import keeps this string-utility module free of DB/model
        # imports at module load.
        from app.models import ConnectedAccount
        acc = await session.get(ConnectedAccount, account_id)
        if acc is not None and (acc.signature or "").strip():
            return acc.signature
    return None


# ---- HTML-signature renderers -----------------------------------------

# Allowlist of tag names a signature may legitimately contain.  Anything
# OUTSIDE this list (including plain-text angle-bracket content like
# "<Acme & Co>") is treated as text and HTML-escaped so mail clients
# render it instead of silently dropping a fake tag.
_ALLOWED_SIG_TAGS = (
    "a|img|br|p|div|span|strong|em|b|i|u|hr|small|sub|sup|"
    "table|tbody|thead|tr|td|th|font|center"
)
_HTML_TAG_RE = re.compile(
    rf"</?(?:{_ALLOWED_SIG_TAGS})\b[^>]*/?>",
    re.IGNORECASE,
)

# Default inline style applied to bare ``<a>`` tags in a signature.  Gmail
# and a handful of mobile clients strip the user-agent default <a>
# styling, so we pin blue + underlined explicitly to keep links readable.
# Only injected when the anchor doesn't carry its own ``style=`` attribute —
# a user who wants a different colour can set ``style="color:#000;…"``
# and we won't override it.
# NOTE: the frontend Insert-link snippet bakes the same value
# (frontend/src/utils/signaturePreview.js DEFAULT_LINK_STYLE) — keep the
# two in sync when changing.
_DEFAULT_LINK_STYLE = "color:#1d4ed8;text-decoration:underline;"

# ``(\s[^>]*)?`` instead of ``\s+[^>]*`` so a bare ``<a>`` (no attributes)
# also matches and gets the default style.
_ANCHOR_OPEN_RE = re.compile(r"<a(\s[^>]*)?>", re.IGNORECASE)
_HAS_STYLE_ATTR_RE = re.compile(r"\bstyle\s*=", re.IGNORECASE)
# Strips quoted attribute VALUES before testing for a style attribute, so
# ``href="...?style=compact"`` doesn't false-positive as "already styled".
_QUOTED_VALUE_RE = re.compile(r'"[^"]*"|\'[^\']*\'')


def _has_style_attribute(attrs: str) -> bool:
    """True when the attribute string carries a real ``style=`` attribute
    (not a ``style=`` substring inside a quoted value like an href URL)."""
    return bool(_HAS_STYLE_ATTR_RE.search(_QUOTED_VALUE_RE.sub("", attrs)))


def _inject_default_link_style(html: str) -> str:
    """Add the default blue+underline style to ``<a>`` tags that don't
    already carry an inline ``style`` attribute.  Idempotent — anchors
    already styled are returned unchanged."""
    def _repl(m: re.Match[str]) -> str:
        attrs = m.group(1) or ""
        if attrs and _has_style_attribute(attrs):
            return m.group(0)  # leave user styling alone
        attrs = attrs.strip()
        if attrs:
            return f'<a {attrs} style="{_DEFAULT_LINK_STYLE}">'
        return f'<a style="{_DEFAULT_LINK_STYLE}">'
    return _ANCHOR_OPEN_RE.sub(_repl, html)


_VOID_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_PARA_OPEN_RE = re.compile(r"<p\b[^>]*>", re.IGNORECASE)
_PARA_CLOSE_RE = re.compile(r"</p\s*>", re.IGNORECASE)
_ANCHOR_RE = re.compile(
    r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_IMG_RE = re.compile(
    r'<img\s+[^>]*src\s*=\s*["\']([^"\']+)["\'][^>]*/?>',
    re.IGNORECASE,
)
_GENERIC_TAG_RE = re.compile(r"<[^>]+>")


def signature_to_html(signature: str | None) -> str:
    """Render the signature for the HTML email body.

    Allowlisted HTML tags (``<a>``, ``<img>``, basic formatting — see
    ``_ALLOWED_SIG_TAGS``) pass through verbatim so they reach the
    recipient's inbox as real HTML.  Everything else — including
    plain-text angle-bracket content like ``<Acme & Co>`` — is treated
    as text: HTML-escaped so mail clients display it instead of dropping
    a fake tag.  Naked newlines outside tags become ``<br>`` so a
    multi-line plain-text signature still renders with line breaks.

    Empty/None signature → empty string."""
    sig = (signature or "").strip()
    if not sig:
        return ""

    def _text_chunk(chunk: str) -> str:
        # quote=False keeps " and ' raw — they're fine in text content
        # and escaping them would garble apostrophes in names.
        return _html.escape(chunk, quote=False).replace("\n", "<br>\n")

    out: list[str] = []
    last_end = 0
    for match in _HTML_TAG_RE.finditer(sig):
        chunk = sig[last_end:match.start()]
        if chunk:
            out.append(_text_chunk(chunk))
        out.append(match.group(0))
        last_end = match.end()
    tail = sig[last_end:]
    if tail:
        out.append(_text_chunk(tail))
    rendered = "".join(out)
    # Pin the default link styling so emails look the same in clients
    # that strip the user-agent default <a> styling (Gmail, some mobile).
    return _inject_default_link_style(rendered)


def signature_to_text(signature: str | None) -> str:
    """Render the signature for the plain-text email body.

    - ``<a href="X">text</a>`` → ``text (X)``
    - ``<img src="X" alt="Y">`` → ``[image: Y — X]`` (or ``[image: X]``
      when no alt attribute).
    - ``<br>`` → newline.
    - ``<p>`` and ``</p>`` → newline (paragraph break).
    - Any other tag is stripped, its inner text content preserved.
    - HTML entities are decoded via stdlib ``html.unescape`` (handles
      every named + numeric entity, and decodes ``&amp;`` last so nested
      escapes like ``&amp;lt;`` aren't double-decoded).

    The result is what a plain-text email client (or someone who's
    disabled HTML rendering) will actually see for the signature block.
    """
    sig = (signature or "").strip()
    if not sig:
        return ""
    out = sig
    # Anchors → "text (URL)" before stripping other tags so we don't
    # lose the href.
    out = _ANCHOR_RE.sub(
        lambda m: f"{_strip_inner_tags(m.group(2)).strip()} ({m.group(1)})",
        out,
    )
    # Images → "[image: alt — URL]" or "[image: URL]".
    def _img_repl(m: re.Match[str]) -> str:
        whole = m.group(0)
        src = m.group(1)
        alt_m = re.search(r'alt\s*=\s*["\']([^"\']*)["\']', whole, re.IGNORECASE)
        alt = (alt_m.group(1).strip() if alt_m else "") or ""
        return f"[image: {alt} — {src}]" if alt else f"[image: {src}]"
    out = _IMG_RE.sub(_img_repl, out)
    # Block / line-break tags → newlines.
    out = _VOID_TAG_RE.sub("\n", out)
    out = _PARA_OPEN_RE.sub("", out)
    out = _PARA_CLOSE_RE.sub("\n", out)
    # Strip remaining tags, keep their inner text.
    out = _GENERIC_TAG_RE.sub("", out)
    out = _html.unescape(out)
    # Collapse runs of 3+ newlines down to 2 — a paragraph break is fine.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _strip_inner_tags(text: str) -> str:
    """Helper for the anchor substitution above — link text might
    contain nested ``<strong>``/``<em>`` etc.; reduce to plain words."""
    return _GENERIC_TAG_RE.sub("", text)


# ---- Shared body + signature renderer ----------------------------------

def render_email_with_signature(
    body: str | None, signature: str | None,
) -> tuple[str, str]:
    """Render ``(html_body, text_body)`` for an outgoing email whose
    signature may contain HTML.

    This is the single entry point BOTH send paths use — the
    research-client one-off send and the bulk campaign send — so an HTML
    signature renders identically everywhere instead of being
    HTML-escaped into visible angle brackets on one path.

    Idempotency: the body may already carry the signature — the bulk
    pipeline merges it at compose time via ``apply_signature``, and a
    user can paste a previously-signed draft into the one-off send form.
    The raw form AND the plain-text form (``signature_to_text``) are both
    checked and stripped off the tail before re-rendering, so the
    signature never appears twice.  When neither form is present, the
    AI's sign-off line is stripped instead (the signature replaces it).
    """
    from app.services.email_template import render_html, render_text

    body = (body or "").rstrip()
    sig = (signature or "").strip()
    if not sig:
        return render_html(body), render_text(body)

    sig_text = signature_to_text(sig)
    if body.endswith(sig):
        stripped = body[: -len(sig)].rstrip()
    elif sig_text and body.endswith(sig_text):
        stripped = body[: -len(sig_text)].rstrip()
    else:
        stripped = strip_signoff(body)

    html_body = render_html(stripped)
    sig_html = signature_to_html(sig)
    html_body = html_body.replace(
        "</body>",
        '<div class="signature" style="margin-top: 1.5em; '
        'padding-top: 1em; border-top: 1px solid #eee;">'
        f"{sig_html}</div>\n</body>",
        1,
    )
    text_body = render_text(stripped).rstrip() + "\n\n" + sig_text
    return html_body, text_body
