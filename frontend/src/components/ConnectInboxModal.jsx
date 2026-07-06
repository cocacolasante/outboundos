import { useState, useEffect, useRef } from 'react';
import { createAccount, updateAccount, testAccount } from '../api/connectedAccounts.js';
import { DEFAULT_LINK_STYLE, signatureToPreviewHtml } from '../utils/signaturePreview.js';


/** Insert ``snippet`` at the current cursor position of ``el`` and call
 *  ``onChange`` with the new full value.  Mutates the textarea's
 *  selection so the cursor lands AFTER the inserted snippet — important
 *  so the user can keep typing without re-clicking. */
function insertAtCursor(el, snippet, onChange) {
  if (!el) {
    onChange((prev) => `${prev || ''}${snippet}`);
    return;
  }
  const start = el.selectionStart ?? el.value.length;
  const end = el.selectionEnd ?? el.value.length;
  const before = el.value.slice(0, start);
  const after = el.value.slice(end);
  const next = `${before}${snippet}${after}`;
  onChange(next);
  // Restore focus + place cursor after the inserted snippet on next tick.
  // (React's re-render will reset the selection so we have to defer.)
  requestAnimationFrame(() => {
    if (document.activeElement !== el) el.focus();
    const pos = start + snippet.length;
    try { el.setSelectionRange(pos, pos); } catch { /* noop */ }
  });
}

const PRESETS = {
  Gmail: { imap_host: 'imap.gmail.com', imap_port: 993, imap_use_ssl: true },
  Outlook: { imap_host: 'outlook.office365.com', imap_port: 993, imap_use_ssl: true },
  Yahoo: { imap_host: 'imap.mail.yahoo.com', imap_port: 993, imap_use_ssl: true },
};

const EMPTY = {
  label: '',
  email_address: '',
  imap_host: '',
  imap_port: 993,
  imap_use_ssl: true,
  username: '',
  password: '',
  signature: '',
};

export default function ConnectInboxModal({ account, onClose, onSaved }) {
  const editing = Boolean(account);
  const [form, setForm] = useState(EMPTY);
  const [showPassword, setShowPassword] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState(null);
  // Signature editor state.  ``signaturePreview`` toggles between the
  // raw HTML textarea and a rendered HTML preview.  The ref is used by
  // the toolbar buttons to insert tags at the textarea's cursor
  // position rather than appending to the end.
  const [signaturePreview, setSignaturePreview] = useState(false);
  const signatureRef = useRef(null);

  function insertSignatureSnippet(snippet) {
    insertAtCursor(
      signatureRef.current, snippet,
      (next) => setForm((f) => ({
        ...f,
        signature: typeof next === 'function' ? next(f.signature) : next,
      })),
    );
  }

  function handleInsertImage() {
    const url = (window.prompt('Image URL (https://…)') || '').trim();
    if (!url) return;
    // Sane size cap so a giant image doesn't blow out the recipient's
    // inbox layout.  Users can tweak the style manually if they want
    // a different size.
    const alt = (window.prompt('Alt text (describe the image for accessibility)', '') || '').trim();
    const altAttr = alt ? ` alt="${alt.replace(/"/g, '&quot;')}"` : '';
    insertSignatureSnippet(
      `<img src="${url}"${altAttr} style="max-width:200px;height:auto;">`,
    );
  }

  function handleInsertLink() {
    const url = (window.prompt('Link URL (https://…)') || '').trim();
    if (!url) return;
    const text = (window.prompt('Link text', url) || url).trim();
    // Inline-style the link: blue + underlined.  Gmail and a couple of
    // mobile clients strip the user-agent default <a> styling, so we
    // pin it explicitly here.  The backend's signature_to_html will
    // also auto-inject this default on any bare <a> the user
    // hand-types (or already has from before this change), but
    // injecting it here too means the WYSIWYG preview in the modal
    // shows the final styling immediately.
    insertSignatureSnippet(
      `<a href="${url}" style="${DEFAULT_LINK_STYLE}">${text}</a>`,
    );
  }

  useEffect(() => {
    if (account) {
      setForm({
        label: account.label || '',
        email_address: account.email_address || '',
        imap_host: account.imap_host || '',
        imap_port: account.imap_port || 993,
        imap_use_ssl: account.imap_use_ssl ?? true,
        username: account.username || '',
        password: '',
        signature: account.signature || '',
      });
    } else {
      setForm(EMPTY);
    }
  }, [account]);

  function update(field, value) {
    setForm((f) => {
      const next = { ...f, [field]: value };
      // Auto-fill username from email if the user hasn't customised it yet.
      if (field === 'email_address' && (!f.username || f.username === f.email_address)) {
        next.username = value;
      }
      return next;
    });
  }

  function applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    setForm((f) => ({ ...f, ...p }));
  }

  async function handleTest() {
    if (!editing) {
      setError('Save the inbox first, then test the connection.');
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const r = await testAccount(account.id);
      setTestResult(r);
    } catch (e) {
      setTestResult({ ok: false, error: e?.message || 'Test failed' });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    setError(null);
    setSaving(true);
    try {
      const payload = { ...form };
      if (editing && !payload.password) delete payload.password;
      let saved;
      if (editing) {
        saved = await updateAccount(account.id, payload);
      } else {
        saved = await createAccount(payload);
      }
      // Auto-run a connection test after save.
      try {
        const r = await testAccount(saved.id);
        setTestResult(r);
      } catch (e) {
        setTestResult({ ok: false, error: e?.message || 'Saved, but test failed' });
      }
      if (onSaved) onSaved(saved);
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Save failed';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div data-testid="modal-overlay" className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <h2 className="m-0 text-lg font-semibold text-slate-900">{editing ? 'Edit inbox' : 'Connect inbox'}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-xl font-medium leading-none bg-transparent border-none cursor-pointer p-1"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label htmlFor="inbox-label" className="block text-sm font-medium text-slate-700 mb-1">Label</label>
            <input
              id="inbox-label"
              value={form.label}
              onChange={(e) => update('label', e.target.value)}
              placeholder="e.g. Work Gmail"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            />
          </div>

          <div>
            <label htmlFor="inbox-email" className="block text-sm font-medium text-slate-700 mb-1">Email address</label>
            <input
              id="inbox-email"
              value={form.email_address}
              onChange={(e) => update('email_address', e.target.value)}
              placeholder="you@example.com"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            />
          </div>

          <div>
            <span className="block text-sm font-medium text-slate-700 mb-2">Preset</span>
            <div className="flex gap-2 flex-wrap">
              {Object.keys(PRESETS).map((p) => (
                <button
                  type="button"
                  key={p}
                  onClick={() => applyPreset(p)}
                  className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors"
                  aria-label={`Use ${p} preset`}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label htmlFor="inbox-imap-host" className="block text-sm font-medium text-slate-700 mb-1">IMAP host</label>
            <input
              id="inbox-imap-host"
              value={form.imap_host}
              onChange={(e) => update('imap_host', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            />
          </div>

          <div className="flex gap-3">
            <div className="flex-1">
              <label htmlFor="inbox-port" className="block text-sm font-medium text-slate-700 mb-1">Port</label>
              <input
                id="inbox-port"
                type="number"
                value={form.imap_port}
                onChange={(e) => update('imap_port', Number(e.target.value))}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
            </div>
            <div className="flex-1 flex items-end pb-2">
              <label className="flex items-center gap-2 text-sm font-medium text-slate-700 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.imap_use_ssl}
                  onChange={(e) => update('imap_use_ssl', e.target.checked)}
                  className="w-4 h-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                />
                Use SSL
              </label>
            </div>
          </div>

          <div>
            <label htmlFor="inbox-username" className="block text-sm font-medium text-slate-700 mb-1">Username</label>
            <input
              id="inbox-username"
              value={form.username}
              onChange={(e) => update('username', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            />
            <p className="mt-1 text-xs text-slate-500">
              For Gmail aliases, set this to your primary mailbox login (not the alias). Aliases share their parent mailbox and cannot log in over IMAP on their own.
            </p>
          </div>

          <div>
            <label htmlFor="inbox-password" className="block text-sm font-medium text-slate-700 mb-1">
              Password{' '}
              {editing && <span className="text-xs text-slate-400 font-normal">(leave blank to keep current)</span>}
            </label>
            <div className="flex gap-2">
              <input
                id="inbox-password"
                type={showPassword ? 'text' : 'password'}
                value={form.password}
                onChange={(e) => update('password', e.target.value)}
                className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors"
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          <div className="flex items-center gap-3 p-4 bg-brand-50 border border-brand-200 rounded-lg text-sm text-brand-800">
            <svg className="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
            </svg>
            <span>
              For Gmail, use an{' '}
              <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer" className="underline">
                App Password
              </a>{' '}
              (not your account password). Requires 2-Step Verification.
              For Outlook, use your regular password or an app password if MFA is enabled.
            </span>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label htmlFor="inbox-signature" className="block text-sm font-medium text-slate-700">
                Email signature
                <span className="text-xs text-slate-400 font-normal"> (optional · supports HTML)</span>
              </label>
              <button
                type="button"
                onClick={() => setSignaturePreview((v) => !v)}
                data-testid="signature-preview-toggle"
                className="text-xs text-brand-600 hover:text-brand-700"
              >
                {signaturePreview ? 'Edit' : 'Preview'}
              </button>
            </div>
            {/* Toolbar — disabled in preview mode since you can't edit the
                rendered HTML.  Buttons insert at the textarea cursor. */}
            <div
              data-testid="signature-toolbar"
              className="flex items-center gap-2 mb-1 p-1 bg-slate-50 border border-slate-300 rounded-t-lg border-b-0"
            >
              <button
                type="button"
                onClick={handleInsertImage}
                disabled={signaturePreview}
                data-testid="signature-insert-image"
                className="px-2 py-1 text-xs font-medium bg-white hover:bg-slate-100 disabled:opacity-50 text-slate-700 border border-slate-300 rounded"
                title="Insert image"
              >
                🖼️ Insert image
              </button>
              <button
                type="button"
                onClick={handleInsertLink}
                disabled={signaturePreview}
                data-testid="signature-insert-link"
                className="px-2 py-1 text-xs font-medium bg-white hover:bg-slate-100 disabled:opacity-50 text-slate-700 border border-slate-300 rounded"
                title="Insert link"
              >
                🔗 Insert link
              </button>
              <span className="text-xs text-slate-400 ml-auto">
                {signaturePreview ? 'Preview (read-only)' : 'HTML allowed'}
              </span>
            </div>
            {signaturePreview ? (
              <div
                data-testid="signature-preview-pane"
                className="w-full px-3 py-2 border border-slate-300 rounded-b-lg text-sm bg-white min-h-[120px]"
                // Trusted input — the workspace admin types this directly.
                // Same trust model as the per-Campaign signature.
                // signatureToPreviewHtml mirrors the backend renderer
                // (newlines only become <br> outside tags, text escaped,
                // bare links auto-styled) so the preview matches the
                // actually-sent email.
                dangerouslySetInnerHTML={{
                  __html: signatureToPreviewHtml(form.signature)
                    || '<span class="text-slate-400">(signature is empty)</span>',
                }}
              />
            ) : (
              <textarea
                ref={signatureRef}
                id="inbox-signature"
                rows={5}
                value={form.signature}
                onChange={(e) => update('signature', e.target.value)}
                data-testid="inbox-signature"
                placeholder={
                  'Best,\nAnthony Colasante\n\n' +
                  '<a href="https://csuitecode.com">csuitecode.com</a> · ' +
                  '<a href="https://calendly.com/anthony">book a call</a>\n' +
                  '<img src="https://example.com/logo.png" style="max-width:120px;">'
                }
                className="w-full px-3 py-2 border border-slate-300 rounded-b-lg text-sm text-slate-900 font-mono leading-relaxed focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
            )}
            <p className="text-xs text-slate-500 mt-1">
              Appended to one-off sends from the Research-a-client tool when this inbox is the
              from-address.  HTML tags (links, images, formatting) render in the recipient's
              inbox; newlines become line breaks.  Leave blank for no signature.
            </p>
          </div>

          {testResult && (
            <div
              data-testid="test-result"
              className={`p-3 rounded-lg text-sm ${testResult.ok ? 'bg-emerald-50 border border-emerald-200 text-emerald-800' : 'bg-red-50 border border-red-200 text-red-800'}`}
            >
              {testResult.ok
                ? `Connection OK${testResult.message_count != null ? ` — ${testResult.message_count} messages in INBOX` : ''}`
                : `Connection failed: ${testResult.error || 'unknown error'}`}
            </div>
          )}

          {error && (
            <div data-testid="modal-error" className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
              {error}
            </div>
          )}
        </div>

        <div className="flex gap-2 justify-end mt-6 pt-4 border-t border-slate-100">
          <button
            type="button"
            onClick={handleTest}
            disabled={testing || !editing}
            className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
