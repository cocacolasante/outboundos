import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { listIntegrations, saveIntegration, testIntegration, registerUnipileWebhooks } from '../api/settings.js';
import { listAccounts, createAccount, testAccount } from '../api/connectedAccounts.js';

const IMAP_PRESETS = {
  Gmail: { imap_host: 'imap.gmail.com', imap_port: 993, imap_use_ssl: true },
  Outlook: { imap_host: 'outlook.office365.com', imap_port: 993, imap_use_ssl: true },
  Yahoo: { imap_host: 'imap.mail.yahoo.com', imap_port: 993, imap_use_ssl: true },
};

function StepIndicator({ steps, current }) {
  return (
    <div className="flex items-center justify-center gap-1 mb-8">
      {steps.map((step, i) => (
        <div key={i} className="flex items-center">
          <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-semibold transition-colors ${
            i < current ? 'bg-emerald-500 text-white' :
            i === current ? 'bg-brand-600 text-white' :
            'bg-slate-200 text-slate-500'
          }`}>
            {i < current ? (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
            ) : i + 1}
          </div>
          {i < steps.length - 1 && (
            <div className={`w-12 h-0.5 mx-1 transition-colors ${i < current ? 'bg-emerald-500' : 'bg-slate-200'}`} />
          )}
        </div>
      ))}
    </div>
  );
}

function StatusBadge({ status }) {
  if (!status) return null;
  const isOk = status === 'ok';
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
      isOk ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'
    }`}>
      {isOk ? (
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      )}
      {isOk ? 'Verified' : 'Failed'}
    </span>
  );
}

function IntegrationStep({ provider, title, description, extraFields, required, integrations, onComplete }) {
  const queryClient = useQueryClient();
  const existing = integrations?.find(i => i.provider === provider);
  const isConfigured = existing?.configured;
  const [form, setForm] = useState({ api_key: '' });
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testStatus, setTestStatus] = useState(existing?.last_test_status || null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (existing?.last_test_status) setTestStatus(existing.last_test_status);
  }, [existing?.last_test_status]);

  async function handleSave() {
    setError(null);
    setSaving(true);
    try {
      await saveIntegration(provider, form);
      setTesting(true);
      try {
        const r = await testIntegration(provider);
        setTestStatus(r.status);
        if (r.status === 'ok') {
          if (provider === 'unipile') {
            try { await registerUnipileWebhooks(); } catch {}
          }
        }
      } catch {
        setTestStatus('failed');
      }
      setTesting(false);
      await queryClient.invalidateQueries({ queryKey: ['integrations'] });
      setForm({ api_key: '' });
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to save');
    } finally {
      setSaving(false);
    }
  }

  const isVerified = testStatus === 'ok' || (isConfigured && existing?.last_test_status === 'ok');

  return (
    <div>
      <div className="flex items-start gap-3 mb-2">
        <h2 className="text-xl font-bold text-slate-900">{title}</h2>
        {required && <span className="mt-1 px-2 py-0.5 bg-amber-100 text-amber-700 text-xs font-semibold rounded-full">Required</span>}
        {!required && <span className="mt-1 px-2 py-0.5 bg-slate-100 text-slate-500 text-xs font-semibold rounded-full">Optional</span>}
      </div>
      <p className="text-sm text-slate-600 mb-5">{description}</p>

      {isVerified ? (
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4 flex items-center gap-3">
          <svg className="w-6 h-6 text-emerald-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div>
            <p className="text-sm font-medium text-emerald-800">{title} is connected and verified</p>
            {existing?.masked && <p className="text-xs text-emerald-600 mt-0.5">Key: {existing.masked}</p>}
          </div>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-lg p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">API Key</label>
            <input
              type="password"
              value={form.api_key}
              onChange={e => setForm(f => ({ ...f, api_key: e.target.value }))}
              placeholder="Paste your API key here"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500"
            />
          </div>
          {extraFields?.map(f => (
            <div key={f.key}>
              <label className="block text-sm font-medium text-slate-700 mb-1">{f.label}</label>
              <input
                type={f.type || 'text'}
                value={form[f.key] || ''}
                onChange={e => setForm(prev => ({ ...prev, [f.key]: e.target.value }))}
                placeholder={f.placeholder}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500"
              />
            </div>
          ))}

          {testStatus === 'failed' && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
              Connection test failed. Please check your API key and try again.
            </div>
          )}
          {error && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{error}</div>
          )}

          <button
            onClick={handleSave}
            disabled={saving || testing || !form.api_key}
            className="w-full px-4 py-2.5 bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving...' : testing ? 'Testing connection...' : 'Save & verify'}
          </button>
        </div>
      )}
    </div>
  );
}

function InboxStep({ accounts, onComplete }) {
  const queryClient = useQueryClient();
  const hasAccount = accounts?.length > 0;
  const [showForm, setShowForm] = useState(!hasAccount);
  const [form, setForm] = useState({
    label: '', email_address: '', imap_host: '', imap_port: 993,
    imap_use_ssl: true, username: '', password: '',
  });
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState(null);

  function update(field, value) {
    setForm(f => {
      const next = { ...f, [field]: value };
      if (field === 'email_address' && (!f.username || f.username === f.email_address)) {
        next.username = value;
      }
      return next;
    });
  }

  function applyPreset(name) {
    const p = IMAP_PRESETS[name];
    if (p) setForm(f => ({ ...f, ...p }));
  }

  async function handleSave() {
    setError(null);
    setSaving(true);
    try {
      const saved = await createAccount(form);
      try {
        const r = await testAccount(saved.id);
        setTestResult(r);
      } catch {
        setTestResult({ ok: false, error: 'Saved, but connection test failed' });
      }
      await queryClient.invalidateQueries({ queryKey: ['connected-accounts'] });
      setShowForm(false);
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <div className="flex items-start gap-3 mb-2">
        <h2 className="text-xl font-bold text-slate-900">Connect an Inbox</h2>
        <span className="mt-1 px-2 py-0.5 bg-blue-100 text-blue-700 text-xs font-semibold rounded-full">Recommended</span>
      </div>
      <p className="text-sm text-slate-600 mb-5">
        Connect your email inbox via IMAP to enable <strong>reply tracking</strong>. When prospects respond to your campaigns, their replies appear in your Replies inbox and are automatically classified by AI.
      </p>

      {hasAccount && !showForm ? (
        <div className="space-y-3">
          {accounts.map(acc => (
            <div key={acc.id} className="bg-emerald-50 border border-emerald-200 rounded-lg p-4 flex items-center gap-3">
              <svg className="w-6 h-6 text-emerald-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div>
                <p className="text-sm font-medium text-emerald-800">{acc.label || 'Inbox'} connected</p>
                <p className="text-xs text-emerald-600">{acc.email_address}</p>
              </div>
            </div>
          ))}
        </div>
      ) : showForm ? (
        <div className="bg-white border border-slate-200 rounded-lg p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">Label</label>
            <input value={form.label} onChange={e => update('label', e.target.value)} placeholder="e.g. Work Gmail"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">Email address</label>
            <input value={form.email_address} onChange={e => update('email_address', e.target.value)} placeholder="you@example.com"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
          </div>
          <div>
            <span className="block text-sm font-medium text-slate-700 mb-2">Provider preset</span>
            <div className="flex gap-2">
              {Object.keys(IMAP_PRESETS).map(p => (
                <button key={p} onClick={() => applyPreset(p)} type="button"
                  className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors">
                  {p}
                </button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">IMAP host</label>
              <input value={form.imap_host} onChange={e => update('imap_host', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">Port</label>
              <input type="number" value={form.imap_port} onChange={e => update('imap_port', Number(e.target.value))}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">Username</label>
            <input value={form.username} onChange={e => update('username', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">Password</label>
            <input type="password" value={form.password} onChange={e => update('password', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500" />
          </div>
          <div className="flex items-center gap-3 p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800">
            <svg className="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
            </svg>
            For Gmail, use an App Password (not your account password). Go to Google Account &rarr; Security &rarr; 2-Step Verification &rarr; App passwords.
          </div>

          {testResult && (
            <div className={`p-3 rounded-lg text-sm ${testResult.ok ? 'bg-emerald-50 border border-emerald-200 text-emerald-700' : 'bg-red-50 border border-red-200 text-red-700'}`}>
              {testResult.ok ? 'Connection successful!' : `Connection failed: ${testResult.error || 'unknown error'}`}
            </div>
          )}
          {error && <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{error}</div>}

          <button onClick={handleSave} disabled={saving || !form.email_address || !form.imap_host || !form.password}
            className="w-full px-4 py-2.5 bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
            {saving ? 'Connecting...' : 'Connect & test'}
          </button>
        </div>
      ) : null}
    </div>
  );
}

const STEPS = ['Welcome', 'Anthropic', 'Brevo', 'Inbox', 'Extras', 'Done'];

export default function Setup() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);

  const { data: integrations, isLoading: intLoading } = useQuery({
    queryKey: ['integrations'],
    queryFn: listIntegrations,
    staleTime: 10_000,
  });

  const { data: accounts, isLoading: accLoading } = useQuery({
    queryKey: ['connected-accounts'],
    queryFn: listAccounts,
    staleTime: 10_000,
  });

  const isLoading = intLoading || accLoading;

  function getIntegration(provider) {
    return integrations?.find(i => i.provider === provider);
  }
  function isVerified(provider) {
    const int = getIntegration(provider);
    return int?.configured && int?.last_test_status === 'ok';
  }

  function canProceed() {
    switch (step) {
      case 0: return true;
      case 1: return isVerified('anthropic');
      case 2: return isVerified('brevo');
      case 3: return true;
      case 4: return true;
      case 5: return true;
      default: return true;
    }
  }

  function nextStep() {
    if (step < STEPS.length - 1) setStep(step + 1);
  }

  function finish() {
    navigate('/campaigns', { replace: true });
  }

  if (isLoading) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center">
        <div className="text-sm text-slate-400">Loading...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50 flex flex-col">
      <nav className="bg-white border-b border-slate-200 px-6 py-4">
        <div className="max-w-2xl mx-auto flex items-center justify-between">
          <div>
            <span className="font-bold text-lg text-slate-900">OutboundOS</span>
            <span className="text-slate-400 text-xs ml-2">Setup wizard</span>
          </div>
          <button onClick={finish} className="text-sm text-slate-500 hover:text-slate-700 font-medium transition-colors">
            Skip setup &rarr;
          </button>
        </div>
      </nav>

      <div className="flex-1 flex items-start justify-center pt-12 pb-16 px-6">
        <div className="w-full max-w-2xl">
          <StepIndicator steps={STEPS} current={step} />

          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-8">
            {step === 0 && <WelcomeStep />}
            {step === 1 && (
              <IntegrationStep
                provider="anthropic"
                title="Connect Anthropic"
                description="Anthropic's Claude AI powers all research and email composition. You need an API key from console.anthropic.com."
                required
                integrations={integrations}
              />
            )}
            {step === 2 && (
              <IntegrationStep
                provider="brevo"
                title="Connect Brevo"
                description="Brevo handles all outbound email delivery and event tracking (opens, clicks, bounces). Sign up at brevo.com and get your API key from SMTP & API settings."
                extraFields={[
                  { key: 'sender_email', label: 'Default sender email', placeholder: 'you@yourdomain.com', type: 'email' },
                  { key: 'sender_name', label: 'Default sender name', placeholder: 'Your Name' },
                ]}
                required
                integrations={integrations}
              />
            )}
            {step === 3 && <InboxStep accounts={accounts || []} />}
            {step === 4 && <ExtrasStep integrations={integrations} />}
            {step === 5 && <DoneStep integrations={integrations} accounts={accounts} />}

            <div className="flex items-center justify-between mt-8 pt-5 border-t border-slate-100">
              {step > 0 ? (
                <button onClick={() => setStep(step - 1)}
                  className="text-sm text-slate-500 hover:text-slate-700 font-medium transition-colors">
                  &larr; Back
                </button>
              ) : <div />}

              {step < STEPS.length - 1 ? (
                <div className="flex items-center gap-3">
                  {(step === 1 || step === 2) && !canProceed() && (
                    <span className="text-xs text-slate-400">Add and verify to continue</span>
                  )}
                  {(step === 3 || step === 4) && !canProceed() && null}
                  {(step === 3 || step === 4) && (
                    <button onClick={nextStep}
                      className="text-sm text-slate-500 hover:text-slate-700 font-medium transition-colors">
                      Skip for now
                    </button>
                  )}
                  <button onClick={nextStep} disabled={!canProceed()}
                    className="px-5 py-2.5 bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
                    Continue
                  </button>
                </div>
              ) : (
                <button onClick={finish}
                  className="px-6 py-2.5 bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold rounded-lg transition-colors">
                  Go to campaigns &rarr;
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function WelcomeStep() {
  return (
    <div className="text-center">
      <div className="w-16 h-16 bg-brand-100 rounded-2xl flex items-center justify-center mx-auto mb-5">
        <svg className="w-8 h-8 text-brand-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
        </svg>
      </div>
      <h2 className="text-2xl font-bold text-slate-900 mb-3">Welcome to OutboundOS</h2>
      <p className="text-slate-600 mb-6 max-w-md mx-auto">
        Let's get your workspace set up. We'll walk you through connecting the services that power your AI-driven outreach.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-left max-w-lg mx-auto">
        <div className="bg-slate-50 rounded-lg p-4">
          <div className="text-brand-600 font-bold text-lg mb-1">1</div>
          <p className="text-sm font-medium text-slate-900">AI Engine</p>
          <p className="text-xs text-slate-500">Anthropic API key for research & writing</p>
        </div>
        <div className="bg-slate-50 rounded-lg p-4">
          <div className="text-brand-600 font-bold text-lg mb-1">2</div>
          <p className="text-sm font-medium text-slate-900">Email Sending</p>
          <p className="text-xs text-slate-500">Brevo API key for deliverability</p>
        </div>
        <div className="bg-slate-50 rounded-lg p-4">
          <div className="text-brand-600 font-bold text-lg mb-1">3</div>
          <p className="text-sm font-medium text-slate-900">Reply Tracking</p>
          <p className="text-xs text-slate-500">Connect your inbox via IMAP</p>
        </div>
      </div>
      <p className="text-xs text-slate-400 mt-6">You can always update these later in Settings &rarr; Integrations</p>
    </div>
  );
}

function ExtrasStep({ integrations }) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-slate-900 mb-2">Optional Integrations</h2>
        <p className="text-sm text-slate-600 mb-6">
          These are optional but unlock additional capabilities. You can add them now or later from Settings.
        </p>
      </div>

      <div className="space-y-5">
        <IntegrationStep
          provider="apollo"
          title="Apollo"
          description="Enriches lead data with company info, tech stack, and more. Used with the 'Deep' research mode for higher-quality personalization."
          integrations={integrations}
        />

        <hr className="border-slate-100" />

        <IntegrationStep
          provider="hunter"
          title="Hunter"
          description="Finds professional email addresses by domain. Useful when you have company domains but not individual email addresses."
          integrations={integrations}
        />

        <hr className="border-slate-100" />

        <IntegrationStep
          provider="unipile"
          title="LinkedIn (Unipile)"
          description="Enables LinkedIn outreach in your sequences — connection requests, DMs, profile views, post reactions, and comments."
          extraFields={[
            { key: 'dsn', label: 'DSN', placeholder: 'api12.unipile.com:13443' },
          ]}
          integrations={integrations}
        />
      </div>
    </div>
  );
}

function DoneStep({ integrations, accounts }) {
  const configured = integrations?.filter(i => i.configured && i.last_test_status === 'ok') || [];
  const hasInbox = accounts?.length > 0;

  return (
    <div className="text-center">
      <div className="w-16 h-16 bg-emerald-100 rounded-2xl flex items-center justify-center mx-auto mb-5">
        <svg className="w-8 h-8 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </div>
      <h2 className="text-2xl font-bold text-slate-900 mb-3">You're all set!</h2>
      <p className="text-slate-600 mb-6 max-w-md mx-auto">
        Your workspace is configured and ready to go. Here's a summary of what's connected:
      </p>

      <div className="text-left max-w-sm mx-auto space-y-2 mb-6">
        {['anthropic', 'brevo', 'apollo', 'hunter', 'unipile'].map(p => {
          const int = integrations?.find(i => i.provider === p);
          const ok = int?.configured && int?.last_test_status === 'ok';
          const labels = { anthropic: 'Anthropic (AI)', brevo: 'Brevo (Email)', apollo: 'Apollo (Enrichment)', hunter: 'Hunter (Email finder)', unipile: 'LinkedIn (Unipile)' };
          return (
            <div key={p} className="flex items-center gap-3 py-2 px-3 rounded-lg bg-slate-50">
              {ok ? (
                <svg className="w-5 h-5 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
              ) : (
                <svg className="w-5 h-5 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 12H4" />
                </svg>
              )}
              <span className={`text-sm ${ok ? 'text-slate-900 font-medium' : 'text-slate-400'}`}>{labels[p]}</span>
            </div>
          );
        })}
        <div className="flex items-center gap-3 py-2 px-3 rounded-lg bg-slate-50">
          {hasInbox ? (
            <svg className="w-5 h-5 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          ) : (
            <svg className="w-5 h-5 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 12H4" />
            </svg>
          )}
          <span className={`text-sm ${hasInbox ? 'text-slate-900 font-medium' : 'text-slate-400'}`}>
            Connected inbox{hasInbox ? ` (${accounts[0].email_address})` : ''}
          </span>
        </div>
      </div>

      <p className="text-sm text-slate-500">Click below to create your first campaign.</p>
    </div>
  );
}
