/**
 * Signature preview rendering — the frontend mirror of the backend's
 * `signature_to_html` (backend/app/services/signature.py).
 *
 * Both signature previews (the Settings inbox editor + the
 * Research-a-client send form) MUST render through this helper so what
 * the user sees matches what the backend actually sends:
 * - Allowlisted HTML tags pass through verbatim.
 * - Plain text (including angle-bracket content like "<Acme & Co>")
 *   is HTML-escaped.
 * - Newlines OUTSIDE tags become <br>; newlines inside multi-line tags
 *   are left alone (a naive global \n→<br> replace breaks tags whose
 *   attributes span lines).
 * - Bare <a> tags get the default blue/underline style injected, same
 *   as the backend does at send time.
 */

/** Default inline link style.  MUST stay in sync with the backend's
 *  _DEFAULT_LINK_STYLE in backend/app/services/signature.py — both
 *  sides have a test asserting the literal so drift breaks CI. */
export const DEFAULT_LINK_STYLE = 'color:#1d4ed8;text-decoration:underline;';

// Mirrors backend _ALLOWED_SIG_TAGS.
const ALLOWED_TAGS =
  'a|img|br|p|div|span|strong|em|b|i|u|hr|small|sub|sup|' +
  'table|tbody|thead|tr|td|th|font|center';
const TAG_RE = new RegExp(`</?(?:${ALLOWED_TAGS})\\b[^>]*/?>`, 'gi');

const ANCHOR_OPEN_RE = /<a(\s[^>]*)?>/gi;
const HAS_STYLE_ATTR_RE = /\bstyle\s*=/i;
const QUOTED_VALUE_RE = /"[^"]*"|'[^']*'/g;

function escapeText(chunk) {
  return chunk
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br>\n');
}

function injectDefaultLinkStyle(html) {
  return html.replace(ANCHOR_OPEN_RE, (whole, attrs) => {
    const a = (attrs || '').trim();
    // Strip quoted values before testing so href="...?style=x" doesn't
    // count as "already styled".
    if (a && HAS_STYLE_ATTR_RE.test(a.replace(QUOTED_VALUE_RE, ''))) {
      return whole;
    }
    return a
      ? `<a ${a} style="${DEFAULT_LINK_STYLE}">`
      : `<a style="${DEFAULT_LINK_STYLE}">`;
  });
}

/** Render a signature string to preview HTML matching the backend's
 *  send-time rendering.  Empty / null → empty string. */
export function signatureToPreviewHtml(signature) {
  const sig = (signature || '').trim();
  if (!sig) return '';
  const out = [];
  let lastEnd = 0;
  // matchAll keeps the regex stateless across calls (TAG_RE is global).
  for (const m of sig.matchAll(TAG_RE)) {
    const chunk = sig.slice(lastEnd, m.index);
    if (chunk) out.push(escapeText(chunk));
    out.push(m[0]);
    lastEnd = m.index + m[0].length;
  }
  const tail = sig.slice(lastEnd);
  if (tail) out.push(escapeText(tail));
  return injectDefaultLinkStyle(out.join(''));
}
