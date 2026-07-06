import { useEffect, useRef, useState } from 'react';

import {
  connectViaUnipile,
  deleteLinkedInAccount,
  getLinkedInAccount,
  importFromUnipile,
  listDiscoverableUnipileAccounts,
  resolveLinkedInChallenge,
  syncUnipileStatus,
  testLinkedInAccount,
} from '../api/linkedinAccounts.js';

// How often to poll /linkedin-accounts/{id}/sync-unipile while waiting for
// the user to complete Unipile's hosted-login.
const SYNC_POLL_MS = 3000;
// Give up polling after this many ms — user can re-open the modal to resume.
const SYNC_POLL_TIMEOUT_MS = 10 * 60 * 1000;

// ---------------------------------------------------------------------

export default function ConnectLinkedInModal({ account, onClose, onSaved }) {
  const editing = Boolean(account);

  const [error, setError] = useState(null);
  const [resolvingChallenge, setResolvingChallenge] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testingNow, setTestingNow] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);

  // ---- Unipile flow state ---------------------------------------------
  const [unipileLabel, setUnipileLabel] = useState('');
  const [unipileLaunching, setUnipileLaunching] = useState(false);
  const [unipileAccountId, setUnipileAccountId] = useState(null);
  const [unipileWaiting, setUnipileWaiting] = useState(false);
  const pollRef = useRef(null);

  // ---- "Import existing Unipile account" state ------------------------
  // For accounts connected via Unipile's dashboard rather than our portal.
  const [discoverable, setDiscoverable] = useState([]);
  const [importing, setImporting] = useState(null);  // unipile_account_id mid-import
  const [showImport, setShowImport] = useState(false);
  const [discoverableError, setDiscoverableError] = useState(null);

  // ---- Effects --------------------------------------------------------

  useEffect(() => {
    if (account?.status === 'challenged') {
      setTestResult({
        ok: false,
        status: 'challenged',
        challenge_url: account.pending_challenge_url,
      });
    } else {
      setTestResult(null);
    }
  }, [account]);

  // Stop polling on unmount.
  useEffect(() => {
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  // Refresh the discoverable list on the "create new" screen. Cheap call —
  // single GET, scoped to unbound rows.  503 means Unipile isn't wired up
  // (DSN / API key missing); we surface that inline rather than silently
  // showing an empty import panel that gives no feedback.
  useEffect(() => {
    if (editing) return;
    let cancelled = false;
    (async () => {
      try {
        const list = await listDiscoverableUnipileAccounts();
        if (cancelled) return;
        setDiscoverable(list || []);
        setDiscoverableError(null);
      } catch (e) {
        if (cancelled) return;
        setDiscoverable([]);
        const status = e?.response?.status;
        const detail = e?.response?.data?.detail || e?.message;
        if (status === 503) {
          setDiscoverableError(
            "Unipile isn't configured for this workspace yet. Set "
            + "UNIPILE_DSN + UNIPILE_API_KEY in .env and force-recreate "
            + "the backend (see the Unipile setup runbook in CLAUDE.md). "
            + "Once configured, both 'Connect via Unipile' and 'Already "
            + "connected in Unipile?' will work."
          );
        } else if (status === 502) {
          setDiscoverableError(
            `Unipile returned an error while listing accounts: ${detail || 'see backend logs'}.`
          );
        } else if (status) {
          setDiscoverableError(`Couldn't load discoverable accounts (${status}): ${detail || 'unknown error'}.`);
        } else {
          setDiscoverableError(
            "Couldn't reach the backend while listing discoverable Unipile accounts."
          );
        }
      }
    })();
    return () => { cancelled = true; };
  }, [editing]);

  async function handleImport(disc) {
    setError(null);
    setImporting(disc.unipile_account_id);
    try {
      const saved = await importFromUnipile({
        unipile_account_id: disc.unipile_account_id,
        label: unipileLabel?.trim() || disc.name || 'LinkedIn (imported)',
      });
      if (onSaved) onSaved(saved);
      onClose();
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Import failed';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    } finally {
      setImporting(null);
    }
  }

  async function handleTestExisting() {
    if (!editing) return;
    setError(null);
    setTestingNow(true);
    try {
      const r = await testLinkedInAccount(account.id);
      setTestResult(r);
      if (onSaved) onSaved({ ...account, status: r.status });
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Test failed';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    } finally {
      setTestingNow(false);
    }
  }

  async function handleReconnect() {
    /* Tear down the dead row + start a fresh hosted-auth flow.  Useful
       when an account is FAILED / RESTRICTED — Unipile's session is
       gone server-side and "Clear challenge state" alone won't help. */
    if (!editing) return;
    const ok = window.confirm(
      `This will disconnect "${account.label}" from Unipile and restart `
      + `the LinkedIn login.  You'll need to re-authorise in a new tab.  Continue?`
    );
    if (!ok) return;
    setError(null);
    setReconnecting(true);
    try {
      // 1. Drop the existing row (also calls Unipile delete_account
      //    so the dead session is cleaned up on their side).
      await deleteLinkedInAccount(account.id);
      // 2. Start a fresh hosted-auth flow under the same label.
      const successUrl = `${window.location.origin}/settings?unipile=success`;
      const failureUrl = `${window.location.origin}/settings?unipile=failure`;
      const { account_id, hosted_url } = await connectViaUnipile({
        label: account.label,
        success_redirect_url: successUrl,
        failure_redirect_url: failureUrl,
      });
      window.open(hosted_url, '_blank', 'noopener,noreferrer');
      // 3. Swap to "create new" mode by signalling the parent — the
      //    polling flow takes over with the new id.
      if (onSaved) onSaved({ id: account_id, status: 'untested' });
      onClose?.();
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Reconnect failed';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    } finally {
      setReconnecting(false);
    }
  }

  async function handleResolveChallenge() {
    if (!editing) return;
    setResolvingChallenge(true);
    try {
      await resolveLinkedInChallenge(account.id);
      setTestResult(null);
      if (onSaved) onSaved({ ...account, status: 'untested', pending_challenge_url: null });
    } catch (e) {
      setError(e?.message || 'Resolve failed');
    } finally {
      setResolvingChallenge(false);
    }
  }

  // ---- Unipile path ---------------------------------------------------

  async function handleUnipileLaunch() {
    if (!unipileLabel.trim()) {
      setError('Give the account a label so you can recognise it later.');
      return;
    }
    setError(null);
    setUnipileLaunching(true);
    try {
      const successUrl = `${window.location.origin}/settings?unipile=success`;
      const failureUrl = `${window.location.origin}/settings?unipile=failure`;
      const { account_id, hosted_url } = await connectViaUnipile({
        label: unipileLabel.trim(),
        success_redirect_url: successUrl,
        failure_redirect_url: failureUrl,
      });
      setUnipileAccountId(account_id);
      setUnipileWaiting(true);
      // Open the hosted login in a new tab.  Pop-up blockers may stop
      // this; user can still click the link manually.
      window.open(hosted_url, '_blank', 'noopener,noreferrer');
      // Start polling for status flip.
      startUnipilePoll(account_id);
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Failed to start Unipile flow';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    } finally {
      setUnipileLaunching(false);
    }
  }

  function startUnipilePoll(accountId) {
    const start = Date.now();
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      if (Date.now() - start > SYNC_POLL_TIMEOUT_MS) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        setUnipileWaiting(false);
        setError('Timed out waiting for Unipile. Close this and reopen to retry.');
        return;
      }
      try {
        const fresh = await syncUnipileStatus(accountId).catch(() => getLinkedInAccount(accountId));
        if (fresh?.status === 'ok') {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setUnipileWaiting(false);
          setTestResult({ ok: true });
          if (onSaved) onSaved(fresh);
        } else if (fresh?.status === 'challenged') {
          // Unipile is still working on it; user might need to enter a code.
          // Keep polling — the next status update will resolve this.
        } else if (fresh?.status === 'failed' || fresh?.status === 'restricted') {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setUnipileWaiting(false);
          setTestResult({ ok: false, error: fresh.last_error || `status=${fresh.status}` });
        }
      } catch (_e) {
        // Transient — keep polling.
      }
    }, SYNC_POLL_MS);
  }

  async function handleCancelPending() {
    // Delete the placeholder row + tell Unipile we're abandoning the link.
    if (!unipileAccountId) return;
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    try {
      await deleteLinkedInAccount(unipileAccountId);
    } catch (_e) {
      // Best-effort; don't block the close.
    }
    setUnipileWaiting(false);
    setUnipileAccountId(null);
    onClose?.();
  }

  const challenged = testResult?.status === 'challenged' || testResult?.meta?.challenged;

  // -- Render ------------------------------------------------------------

  return (
    <div
      data-testid="modal-overlay"
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center mb-5">
          <h2 className="m-0 text-lg font-semibold text-slate-900">
            {editing ? 'Edit LinkedIn account' : 'Connect LinkedIn account'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-xl font-medium leading-none bg-transparent border-none cursor-pointer p-1"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {editing ? (
          <div className="space-y-4">
            <div className="text-sm text-slate-600">
              <div className="font-medium text-slate-900">{account.label}</div>
              <div className="font-mono text-xs">{account.linkedin_email}</div>
              <div className="mt-1 text-xs">
                Status: <span className="font-medium">{account.status}</span>
              </div>
            </div>
            {challenged && (
              <div className="p-3 rounded-lg bg-yellow-50 border border-yellow-200 text-sm text-yellow-900 space-y-2">
                <div className="font-medium">LinkedIn requires verification.</div>
                <p className="text-xs text-yellow-800">
                  Complete any verification Unipile prompts you for in its
                  hosted browser, then click below to clear the challenge state
                  on our side.
                </p>
                <button
                  type="button"
                  onClick={handleResolveChallenge}
                  disabled={resolvingChallenge}
                  className="px-3 py-1.5 text-xs bg-yellow-100 hover:bg-yellow-200 text-yellow-900 border border-yellow-300 rounded-md disabled:opacity-50"
                >
                  {resolvingChallenge ? 'Clearing…' : 'Clear challenge state'}
                </button>
              </div>
            )}
            {testResult && (
              <div
                data-testid="li-test-result"
                className={`p-3 rounded-lg text-sm ${
                  testResult.ok
                    ? 'bg-emerald-50 border border-emerald-200 text-emerald-800'
                    : 'bg-red-50 border border-red-200 text-red-800'
                }`}
              >
                {testResult.ok
                  ? `Connection OK — status: ${testResult.status}.`
                  : `Test failed: ${testResult.error || `status=${testResult.status}`}`}
              </div>
            )}

            {error && (
              <div data-testid="modal-error" className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
                {error}
              </div>
            )}

            <div className="flex flex-wrap gap-2 justify-end pt-4 border-t border-slate-100">
              <button
                type="button"
                onClick={handleReconnect}
                disabled={reconnecting || testingNow}
                className="inline-flex items-center px-3 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Disconnect this account and restart the Unipile hosted-auth flow"
              >
                {reconnecting ? 'Reconnecting…' : 'Reconnect via Unipile'}
              </button>
              <button
                type="button"
                onClick={handleTestExisting}
                disabled={testingNow || reconnecting}
                className="inline-flex items-center px-3 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {testingNow ? 'Testing…' : 'Test connection'}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="inline-flex items-center px-3 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {!unipileWaiting && (
              <>
                <p className="text-sm text-slate-600 leading-relaxed">
                  Click <strong>Connect via Unipile</strong> to open LinkedIn's
                  login form in a new tab. Unipile runs a real desktop Chrome
                  on a residential IP, so LinkedIn sees a normal human session
                  and you won't get challenge-flagged. We never see your
                  password — Unipile handles it.
                </p>
                <div>
                  <label htmlFor="up-label" className="block text-sm font-medium text-slate-700 mb-1">Label</label>
                  <input
                    id="up-label"
                    value={unipileLabel}
                    onChange={(e) => setUnipileLabel(e.target.value)}
                    placeholder="e.g. Anthony — main"
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
                  />
                </div>

                {/* Unipile not configured / API error — show inline so the
                    user knows why both buttons below won't work. */}
                {discoverableError && (
                  <div
                    data-testid="unipile-config-error"
                    className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-3"
                  >
                    <div className="font-medium mb-1">Unipile unavailable</div>
                    {discoverableError}
                  </div>
                )}

                {/* Import existing Unipile accounts ----------------------- */}
                {discoverable.length > 0 && (
                  <div className="border border-slate-200 rounded-lg overflow-hidden" data-testid="unipile-import-section">
                    <button
                      type="button"
                      onClick={() => setShowImport((v) => !v)}
                      className="w-full flex items-center justify-between px-3 py-2 bg-slate-50 hover:bg-slate-100 text-sm font-medium text-slate-700 border-b border-slate-200"
                    >
                      <span>
                        Already connected in Unipile?{' '}
                        <span className="text-slate-500 font-normal">
                          ({discoverable.length} unbound account{discoverable.length === 1 ? '' : 's'} found)
                        </span>
                      </span>
                      <span className="text-xs">{showImport ? '▲' : '▼'}</span>
                    </button>
                    {showImport && (
                      <ul className="divide-y divide-slate-100">
                        {discoverable.map((d) => (
                          <li key={d.unipile_account_id} className="flex items-center justify-between px-3 py-2">
                            <div className="text-sm">
                              <div className="font-medium text-slate-900">{d.name || d.public_identifier || d.unipile_account_id}</div>
                              <div className="text-[11px] text-slate-500 font-mono">
                                {d.public_identifier || d.unipile_account_id}
                                {d.status ? ` · ${d.status}` : ''}
                              </div>
                            </div>
                            <button
                              type="button"
                              onClick={() => handleImport(d)}
                              disabled={importing != null}
                              className="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 text-white text-xs font-medium rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              {importing === d.unipile_account_id ? 'Importing…' : 'Import'}
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                    {showImport && (
                      <div className="px-3 py-2 bg-slate-50 text-[11px] text-slate-500 border-t border-slate-200">
                        Tip: the label field above is applied to the imported row.
                        Leave blank to use the LinkedIn display name.
                      </div>
                    )}
                  </div>
                )}

                <div className="flex gap-2 justify-end pt-2 border-t border-slate-100">
                  <button
                    type="button"
                    onClick={onClose}
                    className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleUnipileLaunch}
                    disabled={unipileLaunching}
                    className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {unipileLaunching ? 'Starting…' : 'Connect via Unipile'}
                  </button>
                </div>
              </>
            )}
            {unipileWaiting && (
              <div className="space-y-3">
                <div className="p-4 rounded-lg bg-brand-50 border border-brand-200 text-sm text-brand-900">
                  <div className="font-medium mb-1">Waiting for you to finish in the new tab…</div>
                  <p className="text-xs">
                    A Unipile login window should have opened. Complete the
                    LinkedIn login there. When you're done, this modal will
                    refresh automatically.
                  </p>
                </div>
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={handleCancelPending}
                    className="text-xs text-slate-500 underline hover:text-slate-700"
                  >
                    Cancel and clean up
                  </button>
                </div>
              </div>
            )}
            {testResult?.ok && (
              <div
                data-testid="li-test-result"
                className="p-3 rounded-lg text-sm bg-emerald-50 border border-emerald-200 text-emerald-800"
              >
                Connection OK — Unipile is now driving this LinkedIn account.
              </div>
            )}
            {testResult?.ok === false && !challenged && (
              <div
                data-testid="li-test-result"
                className="p-3 rounded-lg text-sm bg-red-50 border border-red-200 text-red-800"
              >
                Connection failed: {testResult.error || 'unknown error'}
              </div>
            )}
            {error && (
              <div data-testid="modal-error" className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
                {error}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
