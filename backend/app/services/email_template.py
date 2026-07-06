"""Render composed text into a minimal responsive HTML email + plain text.

The body may contain intentional inline HTML — links/images the user inserted
via the rich-text toolbar, or a signature baked in at compose time.  Those
allowlisted tags pass through verbatim so they reach the inbox as real HTML;
everything else is HTML-escaped so a stray ``<Acme & Co>`` shows literally
instead of being dropped (or, worse, an inserted ``<a href>`` link rendering as
raw angle-bracket text — the "it looks like spam" symptom).
"""
from __future__ import annotations

import html
import re

# Bare http(s) URLs (outside any tag/attribute) for auto-linking.
_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")

# --- Allowlisted inline HTML (shared with the signature renderer) ----------
# Tags an email body / signature may legitimately contain.  Anything outside
# this list is treated as plain text and escaped.
_ALLOWED_TAGS = (
    "a|img|br|p|div|span|strong|em|b|i|u|hr|small|sub|sup|"
    "table|tbody|thead|tr|td|th|font|center"
)
_HTML_TAG_RE = re.compile(rf"</?(?:{_ALLOWED_TAGS})\b[^>]*/?>", re.IGNORECASE)

# Default inline <a> style (blue + underline) for clients that strip the
# user-agent default link styling (Gmail, some mobile).  Mirrored in
# frontend/src/utils/signaturePreview.js (DEFAULT_LINK_STYLE) — keep in sync.
_DEFAULT_LINK_STYLE = "color:#1d4ed8;text-decoration:underline;"
_ANCHOR_OPEN_RE = re.compile(r"<a(\s[^>]*)?>", re.IGNORECASE)
_ANCHOR_CLOSE_RE = re.compile(r"</a\s*>", re.IGNORECASE)
_HAS_STYLE_ATTR_RE = re.compile(r"\bstyle\s*=", re.IGNORECASE)
# Strip quoted attribute VALUES before testing for a style attribute, so
# ``href="...?style=x"`` doesn't false-positive as "already styled".
_QUOTED_VALUE_RE = re.compile(r'"[^"]*"|\'[^\']*\'')


def _has_style_attribute(attrs: str) -> bool:
    """True when ``attrs`` carries a real ``style=`` attribute (not a
    ``style=`` substring inside a quoted value like an href URL)."""
    return bool(_HAS_STYLE_ATTR_RE.search(_QUOTED_VALUE_RE.sub("", attrs)))


def inject_default_link_style(html_str: str) -> str:
    """Add the default blue+underline style to ``<a>`` tags that don't already
    carry an inline ``style`` attribute.  Idempotent."""
    def _repl(m: re.Match[str]) -> str:
        attrs = m.group(1) or ""
        if attrs and _has_style_attribute(attrs):
            return m.group(0)  # leave user styling alone
        attrs = attrs.strip()
        if attrs:
            return f'<a {attrs} style="{_DEFAULT_LINK_STYLE}">'
        return f'<a style="{_DEFAULT_LINK_STYLE}">'
    return _ANCHOR_OPEN_RE.sub(_repl, html_str)


def _autolink_text(text: str) -> str:
    """Escape a plain-text span and turn bare URLs into ``<a>`` links."""
    urls = _URL_RE.findall(text)
    masked = text
    for i, url in enumerate(urls):
        masked = masked.replace(url, f"\x00URL{i}\x00", 1)
    # quote=False keeps " and ' raw — fine in text content, and escaping them
    # would garble apostrophes in names.
    escaped = html.escape(masked, quote=False)
    for i, url in enumerate(urls):
        safe = html.escape(url, quote=True)
        escaped = escaped.replace(f"\x00URL{i}\x00", f'<a href="{safe}">{safe}</a>', 1)
    return escaped


def render_inline(text: str) -> str:
    """Render body text that MAY contain allowlisted inline HTML.

    Allowlisted tags pass through verbatim (so inserted links/images render as
    real HTML); plain-text spans are escaped, with bare URLs auto-linked — but
    NOT when already inside an ``<a>`` (no nested anchors).  Newlines in text
    spans become ``<br>``.
    """
    out: list[str] = []
    last = 0
    in_anchor = 0
    for m in _HTML_TAG_RE.finditer(text):
        chunk = text[last:m.start()]
        if chunk:
            esc = html.escape(chunk, quote=False) if in_anchor else _autolink_text(chunk)
            out.append(esc.replace("\n", "<br>"))
        tag = m.group(0)
        out.append(tag)
        if _ANCHOR_OPEN_RE.fullmatch(tag):
            in_anchor += 1
        elif _ANCHOR_CLOSE_RE.fullmatch(tag):
            in_anchor = max(0, in_anchor - 1)
        last = m.end()
    tail = text[last:]
    if tail:
        esc = html.escape(tail, quote=False) if in_anchor else _autolink_text(tail)
        out.append(esc.replace("\n", "<br>"))
    return inject_default_link_style("".join(out))


def render_html(body_text: str) -> str:
    """Convert an email body into responsive HTML.

    Paragraphs split on blank lines.  Within a paragraph, allowlisted HTML
    passes through, bare URLs auto-link, and newlines render as ``<br>``.
    """
    paragraphs = [p for p in body_text.split("\n\n") if p.strip()]
    body = "\n".join(f"<p>{render_inline(p)}</p>" for p in paragraphs)
    return (
        "<!DOCTYPE html>\n"
        '<html>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "</head>\n"
        '<body style="font-family: -apple-system, BlinkMacSystemFont, '
        "'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; max-width: 600px; "
        'margin: 0 auto; padding: 20px; color: #333; line-height: 1.5;">\n'
        f"{body}\n"
        "</body>\n</html>"
    )


def render_text(body_text: str) -> str:
    """Plain-text version is the body unchanged."""
    return body_text
