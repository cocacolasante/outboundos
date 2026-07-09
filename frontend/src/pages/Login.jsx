import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { login, register, forgotPassword, resetPassword, acceptInvite } from '../api/auth.js';

/**
 * Thin auth screen (multi-tenancy Phase 1): sign in / create workspace /
 * forgot / reset in one card.  The full account-management UI lands in
 * Phase 6 — this exists so the app stays drivable behind the auth gate.
 *
 * Reset mode activates when the password-reset email links back with
 * ?reset_token=…
 */
export default function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const resetToken = searchParams.get('reset_token');
  const inviteToken = searchParams.get('invite_token');
  const inviteCode = searchParams.get('invite_code');
  const modeParam = searchParams.get('mode');

  // 'login' | 'signup' | 'forgot' | 'reset' | 'invite'
  const [mode, setMode] = useState(
    inviteToken ? 'invite' : resetToken ? 'reset' : modeParam === 'signup' ? 'signup' : 'login'
  );
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [tenantName, setTenantName] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setNotice('');
    setBusy(true);
    try {
      if (mode === 'login') {
        await login({ email, password });
      } else if (mode === 'signup') {
        await register({ email, password, tenantName, inviteCode });
      } else if (mode === 'forgot') {
        await forgotPassword(email);
        setNotice('If that email has an account, a reset link is on its way.');
        setBusy(false);
        return;
      } else if (mode === 'reset') {
        await resetPassword({ token: resetToken, password });
        setNotice('Password updated — sign in with your new password.');
        setMode('login');
        setPassword('');
        setBusy(false);
        return;
      } else if (mode === 'invite') {
        await acceptInvite({ token: inviteToken, password });
        setNotice('You\u2019re in — sign in with your new password.');
        setMode('login');
        setPassword('');
        setBusy(false);
        return;
      }
      // Login / signup succeeded: refresh the auth query and enter the app.
      await queryClient.invalidateQueries({ queryKey: ['auth-me'] });
      navigate(mode === 'signup' ? '/setup' : '/campaigns', { replace: true });
    } catch (err) {
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Something went wrong — try again.');
      setBusy(false);
    }
  }

  const heading = {
    login: 'Sign in',
    signup: 'Create your workspace',
    forgot: 'Reset your password',
    reset: 'Choose a new password',
    invite: 'Join the workspace',
  }[mode];

  const submitLabel = {
    login: busy ? 'Signing in…' : 'Sign in',
    signup: busy ? 'Creating…' : 'Create workspace',
    forgot: busy ? 'Sending…' : 'Email me a reset link',
    reset: busy ? 'Saving…' : 'Set new password',
    invite: busy ? 'Joining…' : 'Set password & join',
  }[mode];

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <p className="text-center font-bold text-xl tracking-tight text-slate-900 mb-6">
          OutboundOS
        </p>
        <form
          onSubmit={handleSubmit}
          data-testid="login-form"
          className="bg-white rounded-card shadow-card border border-slate-200 p-6 space-y-4"
        >
          <h1 className="text-lg font-semibold text-slate-900">{heading}</h1>

          {inviteCode && mode === 'signup' && (
            <p className="text-sm text-brand-600 bg-brand-50 border border-brand-200 rounded-md px-3 py-2">
              You've been invited! Create your account to get started — no credit card required.
            </p>
          )}

          {error && (
            <p data-testid="login-error" className="text-sm text-danger-600" role="alert">
              {error}
            </p>
          )}
          {notice && (
            <p data-testid="login-notice" className="text-sm text-success-600">
              {notice}
            </p>
          )}

          {mode !== 'reset' && mode !== 'invite' && (
            <label className="block">
              <span className="text-sm font-medium text-slate-700">Email</span>
              <input
                type="email"
                required
                autoComplete="email"
                data-testid="login-email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              />
            </label>
          )}

          {mode === 'signup' && (
            <label className="block">
              <span className="text-sm font-medium text-slate-700">
                Workspace name <span className="text-slate-400">(optional)</span>
              </span>
              <input
                type="text"
                data-testid="signup-tenant-name"
                value={tenantName}
                onChange={(e) => setTenantName(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              />
            </label>
          )}

          {mode !== 'forgot' && (
            <label className="block">
              <span className="text-sm font-medium text-slate-700">
                {mode === 'reset' || mode === 'invite' ? 'New password' : 'Password'}
              </span>
              <input
                type="password"
                required
                minLength={mode === 'login' ? undefined : 8}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                data-testid="login-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              />
            </label>
          )}

          <button
            type="submit"
            disabled={busy}
            data-testid="login-submit"
            className="w-full rounded-md bg-brand-600 hover:bg-brand-700 disabled:opacity-60 text-white text-sm font-medium px-4 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2"
          >
            {submitLabel}
          </button>

          <div className="flex items-center justify-between text-sm pt-1">
            {mode === 'login' ? (
              <>
                <button
                  type="button"
                  data-testid="login-mode-signup"
                  onClick={() => { setMode('signup'); setError(''); setNotice(''); }}
                  className="text-brand-600 hover:text-brand-700"
                >
                  Create a workspace
                </button>
                <button
                  type="button"
                  data-testid="login-mode-forgot"
                  onClick={() => { setMode('forgot'); setError(''); setNotice(''); }}
                  className="text-slate-500 hover:text-slate-700"
                >
                  Forgot password?
                </button>
              </>
            ) : (
              <button
                type="button"
                data-testid="login-mode-login"
                onClick={() => { setMode('login'); setError(''); setNotice(''); }}
                className="text-brand-600 hover:text-brand-700"
              >
                Back to sign in
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
