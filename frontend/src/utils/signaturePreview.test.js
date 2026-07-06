import { describe, it, expect } from 'vitest';
import {
  DEFAULT_LINK_STYLE,
  signatureToPreviewHtml,
} from './signaturePreview.js';

describe('signatureToPreviewHtml — mirrors backend signature_to_html', () => {
  it('converts newlines OUTSIDE tags to <br>', () => {
    const out = signatureToPreviewHtml('Best,\nAnthony\ncsuitecode.com');
    expect(out.match(/<br>/g)).toHaveLength(2);
  });

  it('leaves newlines INSIDE multi-line tags alone (no broken markup)', () => {
    // Attributes spanning lines — common when pasting from an HTML editor.
    const sig = '<img src="https://x/logo.png"\n  style="max-width:120px;">\nAnthony';
    const out = signatureToPreviewHtml(sig);
    // The tag survives intact — no <br> injected inside it.
    expect(out).toContain('<img src="https://x/logo.png"\n  style="max-width:120px;">');
    // The newline AFTER the tag did become a <br>.
    expect(out).toContain('<br>\nAnthony');
  });

  it('escapes plain-text angle brackets instead of treating them as tags', () => {
    const out = signatureToPreviewHtml('CEO <Acme & Co>');
    expect(out).toContain('&lt;Acme &amp; Co&gt;');
    expect(out).not.toContain('<Acme');
  });

  it('passes allowlisted tags through verbatim', () => {
    const out = signatureToPreviewHtml('<strong>Anthony</strong>');
    expect(out).toContain('<strong>Anthony</strong>');
  });

  it('injects the default style on bare anchors', () => {
    const out = signatureToPreviewHtml('<a href="https://x.com">x</a>');
    expect(out).toContain(`style="${DEFAULT_LINK_STYLE}"`);
  });

  it('does not get fooled by style= inside an href URL', () => {
    const out = signatureToPreviewHtml('<a href="https://x.com/book?style=compact">book</a>');
    // Default style still injected — ?style= in the URL is not a style attr.
    expect(out).toContain(DEFAULT_LINK_STYLE);
  });

  it('leaves user-styled anchors alone', () => {
    const out = signatureToPreviewHtml('<a href="https://x.com" style="color:#000;">x</a>');
    expect(out).toContain('style="color:#000;"');
    expect(out).not.toContain('#1d4ed8');
  });

  it('empty / null → empty string', () => {
    expect(signatureToPreviewHtml('')).toBe('');
    expect(signatureToPreviewHtml(null)).toBe('');
    expect(signatureToPreviewHtml('   ')).toBe('');
  });

  it('DEFAULT_LINK_STYLE matches the backend _DEFAULT_LINK_STYLE literal', () => {
    // Drift guard — backend/app/services/signature.py has the same
    // assertion on its side.  If one side changes without the other,
    // one of the two tests fails.
    expect(DEFAULT_LINK_STYLE).toBe('color:#1d4ed8;text-decoration:underline;');
  });
});
