import { useEffect, useState, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { listAccounts } from '../api/connectedAccounts.js';
import { listLinkedInAccounts } from '../api/linkedinAccounts.js';
import { createCampaign, getCampaign, getPreview, getPreviewProgress, listCampaigns } from '../api/campaigns.js';
import ConnectInboxModal from '../components/ConnectInboxModal.jsx';
import LeadUpload from '../components/LeadUpload.jsx';
import ScheduleConfig from '../components/ScheduleConfig.jsx';
import { EmbeddedSequenceBuilder } from './SequenceBuilder.jsx';

const TONES = ['Professional', 'Friendly', 'Direct', 'Conversational', 'Formal'];

const RESEARCH_MODES = [
  { value: 'fast', label: 'Fast (web only, ~10s/lead)' },
  { value: 'deep', label: 'Deep (+Apollo, ~45s/lead)' },
  { value: 'none', label: 'None (no research, AI writes from name + company)' },
  { value: 'template', label: 'Template (no AI, you write it)' },
];

const DEFAULT_FORM = {
  name: '',
  goal: '',
  tone: 'Professional',
  sender_name: '',
  sender_email: '',
  research_mode: 'fast',
  template_subject: '',
  template_body: '',
  sample_count: 5,
  connected_account_id: '',
  linkedin_account_id: '',
  schedule_days: [0, 1, 2, 3, 4],
  schedule_time_start: '09:00',
  schedule_time_end: '17:00',
  schedule_timezone: 'UTC',
  max_per_hour: null,
  max_per_day: null,
  min_delay_seconds: 60,
  min_delay_unit: 'seconds',
  retarget_source_campaign_id: '',
};

function StatusBadge({ status }) {
  if (!status) return null;
  const classes = {
    untested: 'bg-yellow-100 text-yellow-700',
    ok: 'bg-emerald-100 text-emerald-700',
    failed: 'bg-red-100 text-red-600',
  };
  const labels = { untested: 'Untested', ok: 'Connected', failed: 'Failed' };
  return (
    <span
      data-testid="inbox-status"
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${classes[status] || classes.untested}`}
    >
      {labels[status] || status}
    </span>
  );
}

// --------------------------------------------------------------------------
// Step 1: campaign details
// --------------------------------------------------------------------------

function Step1({ form, setForm, onSubmit, submitting, error, accounts, linkedinAccounts, allCampaigns = [], onConnectInbox }) {
  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }));
  }

  const selectedAccount = accounts.find((a) => a.id === form.connected_account_id);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
      className="space-y-6"
    >
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Campaign details</h2>
        <div className="space-y-4">
          <div>
            <label htmlFor="campaign-name" className="block text-sm font-medium text-slate-700 mb-1">Name</label>
            <input
              id="campaign-name"
              required
              value={form.name}
              onChange={(e) => update('name', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            />
          </div>

          <div>
            <label htmlFor="campaign-goal" className="block text-sm font-medium text-slate-700 mb-1">Goal</label>
            <textarea
              id="campaign-goal"
              required
              rows={3}
              placeholder="What is the goal of this campaign? E.g. Book a demo, announce a product, invite to event"
              value={form.goal}
              onChange={(e) => update('goal', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white font-[inherit]"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="campaign-tone" className="block text-sm font-medium text-slate-700 mb-1">Tone</label>
              <select
                id="campaign-tone"
                value={form.tone}
                onChange={(e) => update('tone', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              >
                {TONES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="campaign-sample-count" className="block text-sm font-medium text-slate-700 mb-1">Sample count</label>
              <input
                id="campaign-sample-count"
                type="number"
                min="1"
                required
                value={form.sample_count}
                onChange={(e) => update('sample_count', Number(e.target.value))}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="campaign-sender-name" className="block text-sm font-medium text-slate-700 mb-1">Sender name</label>
              <input
                id="campaign-sender-name"
                required
                value={form.sender_name}
                onChange={(e) => update('sender_name', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
            </div>
            <div>
              <label htmlFor="campaign-sender-email" className="block text-sm font-medium text-slate-700 mb-1">Sender email</label>
              <input
                id="campaign-sender-email"
                type="email"
                required
                value={form.sender_email}
                onChange={(e) => update('sender_email', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
              />
            </div>
          </div>

          <div>
            <label htmlFor="retarget-source" className="block text-sm font-medium text-slate-700 mb-1">
              Retarget a previous campaign <span className="text-slate-400 font-normal">(optional)</span>
            </label>
            <select
              id="retarget-source"
              data-testid="retarget-source-select"
              value={form.retarget_source_campaign_id}
              onChange={(e) => update('retarget_source_campaign_id', e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white"
            >
              <option value="">No — fresh campaign from new leads</option>
              {allCampaigns.map((c) => (
                <option key={c.id} value={c.id}>Retarget engaged leads from: {c.name}</option>
              ))}
            </select>
            {form.retarget_source_campaign_id && (
              <p className="text-xs text-slate-500 mt-1" data-testid="retarget-hint">
                Leads who clicked a link (or connected on LinkedIn) will be copied in, and the AI
                will reference what they engaged with. Research is skipped (existing leads).
              </p>
            )}
          </div>

          <div>
            <span className="block text-sm font-medium text-slate-700 mb-2">Research mode</span>
            <div role="radiogroup" className="flex flex-wrap gap-2">
              {RESEARCH_MODES.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={form.research_mode === value}
                  onClick={() => update('research_mode', value)}
                  className={`px-4 py-2 rounded-full text-sm border font-medium transition-colors ${
                    form.research_mode === value
                      ? 'bg-brand-600 text-white border-brand-600'
                      : 'border-slate-300 text-slate-600 hover:border-brand-400 bg-white'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            {form.research_mode === 'none' && (
              <p className="mt-2 text-xs text-slate-500">
                Skips all lead research. The AI still writes each email from
                the lead's name and company only (one Anthropic call per lead).
              </p>
            )}
            {form.research_mode === 'template' && (
              <div className="mt-4 space-y-3 p-4 bg-slate-50 border border-slate-200 rounded-lg">
                <p className="text-xs text-slate-500 m-0">
                  No AI is used. The exact text below is sent to every lead with
                  merge fields filled in. Use <code>{'{{first_name}}'}</code>,{' '}
                  <code>{'{{company}}'}</code>, any CSV column header, or an inline
                  fallback like <code>{'{{first_name|there}}'}</code>.
                </p>
                <div>
                  <label htmlFor="template-subject" className="block text-sm font-medium text-slate-700 mb-1">Subject</label>
                  <input
                    id="template-subject"
                    value={form.template_subject}
                    onChange={(e) => update('template_subject', e.target.value)}
                    placeholder="Quick question about {{company}}"
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
                  />
                </div>
                <div>
                  <label htmlFor="template-body" className="block text-sm font-medium text-slate-700 mb-1">Body</label>
                  <textarea
                    id="template-body"
                    rows={8}
                    value={form.template_body}
                    onChange={(e) => update('template_body', e.target.value)}
                    placeholder={'Hi {{first_name|there}},\n\nI noticed {{company}} is ...\n\nWould you be open to a quick chat?\n\n- ' + (form.sender_name || 'Your name')}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white font-[inherit]"
                  />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
        <h2 className="text-base font-semibold text-slate-900 mb-1">
          Reply tracking{' '}
          <span className="text-slate-400 font-normal text-sm">(optional)</span>
        </h2>
        <p className="text-sm text-slate-500 mb-4">
          Connect an inbox to automatically detect when leads reply to your emails.
        </p>

        {accounts.length === 0 ? (
          <div data-testid="no-inbox-prompt" className="p-4 bg-slate-50 border border-slate-200 rounded-lg">
            <p className="text-sm text-slate-600 m-0 mb-3">No inboxes connected yet.</p>
            <button
              type="button"
              onClick={onConnectInbox}
              className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
            >
              Connect an inbox
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-3">
            <select
              aria-label="Reply tracking inbox"
              value={form.connected_account_id}
              onChange={(e) => update('connected_account_id', e.target.value)}
              className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
            >
              <option value="">No reply tracking</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label} ({a.email_address})
                </option>
              ))}
            </select>
            {selectedAccount && <StatusBadge status={selectedAccount.last_test_status} />}
          </div>
        )}
      </div>

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
        <h2 className="text-base font-semibold text-slate-900 mb-1">
          LinkedIn account{' '}
          <span className="text-slate-400 font-normal text-sm">(optional)</span>
        </h2>
        <p className="text-sm text-slate-500 mb-4">
          Required only if this campaign's sequence includes LinkedIn nodes.
        </p>
        {linkedinAccounts.length === 0 ? (
          <div className="p-4 bg-slate-50 border border-slate-200 rounded-lg text-sm text-slate-600">
            No LinkedIn accounts connected. Add one in{' '}
            <a className="underline text-brand-600" href="/settings">Settings → LinkedIn accounts</a>.
          </div>
        ) : (
          <select
            aria-label="LinkedIn account"
            value={form.linkedin_account_id}
            onChange={(e) => update('linkedin_account_id', e.target.value)}
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          >
            <option value="">No LinkedIn actions</option>
            {linkedinAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label} ({a.linkedin_email}) — {a.status}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Schedule</h2>
        <ScheduleConfig value={form} onChange={(next) => setForm(next)} />
      </div>

      {error && (
        <div data-testid="step1-error" className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800">{error}</div>
      )}

      <div className="flex justify-end">
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="step1-submit"
        >
          {submitting ? 'Creating…' : 'Next: build sequence →'}
        </button>
      </div>
    </form>
  );
}

// --------------------------------------------------------------------------
// Step 3: progress poll + auto-redirect
// --------------------------------------------------------------------------

function Step3({ campaignId, onComplete }) {
  const { data: progress } = useQuery({
    queryKey: ['preview-progress', campaignId],
    queryFn: () => getPreviewProgress(campaignId),
    refetchInterval: 5000,
  });
  const { data: preview } = useQuery({
    queryKey: ['preview', campaignId],
    queryFn: () => getPreview(campaignId),
    refetchInterval: 5000,
  });
  const { data: campaign } = useQuery({
    queryKey: ['campaign', campaignId],
    queryFn: () => getCampaign(campaignId),
  });

  useEffect(() => {
    if (preview?.all_ready) onComplete();
  }, [preview, onComplete]);

  const total = progress?.total_leads ?? 0;
  const researched = progress?.researched ?? 0;
  const composed = progress?.composed ?? 0;
  const pct = total ? Math.min(100, Math.round((composed / total) * 100)) : 0;

  // Tailor the wording to the campaign's generation mode. While the campaign
  // is still loading (mode undefined) show neutral copy rather than wrongly
  // implying research is happening.
  const mode = campaign?.research_mode;
  let heading = 'Preparing emails…';
  let detail = `${composed} of ${total} ready`;
  if (mode === 'template') {
    heading = 'Rendering your templated emails…';
    detail = `${composed} of ${total} rendered`;
  } else if (mode === 'none') {
    heading = 'Composing emails…';
    detail = `${composed} of ${total} composed`;
  } else if (mode === 'fast' || mode === 'deep') {
    heading = 'Researching and composing emails…';
    detail = `${composed} of ${total} composed · ${researched} researched`;
  }

  return (
    <div data-testid="step3" className="bg-white rounded-xl border border-slate-200 shadow-sm p-8 text-center">
      <div className="w-12 h-12 mx-auto mb-4 bg-brand-50 rounded-full flex items-center justify-center">
        <svg className="w-6 h-6 text-brand-600 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
        </svg>
      </div>
      <h2 className="text-lg font-semibold text-slate-900 mb-2">{heading}</h2>
      <p className="text-sm text-slate-500 mb-4">
        {detail}
      </p>
      <div className="h-3 bg-slate-200 rounded-full overflow-hidden mx-auto max-w-sm mb-4">
        <div
          data-testid="progress-bar"
          className="h-full bg-brand-600 rounded-full transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-sm text-slate-400">
        This page will auto-advance once the sample emails are ready for review.
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------
// Container
// --------------------------------------------------------------------------

export default function CampaignCreate() {
  // Wizard state lives partly in the URL so an accidental F5 mid-flow
  // resumes where the user left off instead of restarting at step 1 and
  // creating a duplicate campaign on the next submit.
  //   /campaigns/new                    -> step 1, no campaign yet
  //   /campaigns/new?id=<uuid>&step=2   -> resume at step 2 on that campaign
  const [searchParams, setSearchParams] = useSearchParams();
  const urlId = searchParams.get('id');
  const urlStep = parseInt(searchParams.get('step') || '1', 10);

  // Initial step: clamp to a valid range, and don't allow step 1 when we
  // already have an id (the campaign was created — replaying step 1 would
  // make a duplicate).
  const initialStep = (() => {
    const s = Number.isFinite(urlStep) ? urlStep : 1;
    if (urlId && s < 2) return 2;
    return Math.min(Math.max(s, 1), 4);
  })();

  const [step, setStepRaw] = useState(initialStep);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [campaignId, setCampaignIdRaw] = useState(urlId || null);
  const [showInboxModal, setShowInboxModal] = useState(false);
  const [error, setError] = useState(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Wrapped setters that also update the URL search params so refresh
  // and the browser back/forward buttons just work.
  const setStep = useCallback((nextStep) => {
    setStepRaw(nextStep);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('step', String(nextStep));
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const setCampaignId = useCallback((nextId) => {
    setCampaignIdRaw(nextId);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (nextId) next.set('id', String(nextId));
      else next.delete('id');
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const { data: accounts = [] } = useQuery({
    queryKey: ['connected-accounts'],
    queryFn: listAccounts,
  });
  const { data: linkedinAccounts = [] } = useQuery({
    queryKey: ['linkedin-accounts'],
    queryFn: listLinkedInAccounts,
  });
  const { data: allCampaigns = [] } = useQuery({
    queryKey: ['campaigns'],
    queryFn: listCampaigns,
  });

  const createMutation = useMutation({
    mutationFn: createCampaign,
    onSuccess: (data) => {
      setCampaignId(data.id);
      setStep(2);  // → Sequence
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message || 'Failed to create campaign';
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail));
    },
  });

  function buildPayload() {
    const isTemplate = form.research_mode === 'template';
    return {
      ...form,
      // Only carry template copy when the campaign is actually templated, so
      // switching modes after typing doesn't leave stale text on the record.
      template_subject: isTemplate ? form.template_subject || null : null,
      template_body: isTemplate ? form.template_body || null : null,
      connected_account_id: form.connected_account_id || null,
      linkedin_account_id: form.linkedin_account_id || null,
      retarget_source_campaign_id: form.retarget_source_campaign_id || null,
      max_per_hour: form.max_per_hour || null,
      max_per_day: form.max_per_day || null,
      schedule_time_start:
        form.schedule_time_start.length === 5
          ? `${form.schedule_time_start}:00`
          : form.schedule_time_start,
      schedule_time_end:
        form.schedule_time_end.length === 5
          ? `${form.schedule_time_end}:00`
          : form.schedule_time_end,
    };
  }

  function handleStep1Submit() {
    setError(null);
    if (form.research_mode === 'template' && !form.template_body.trim()) {
      setError('Template body is required when using template mode.');
      return;
    }
    createMutation.mutate(buildPayload());
  }

  function onSequenceDone() {
    setStep(3);  // → Upload leads
  }

  function onUploadComplete(result) {
    // A non-email start node has no sample emails to preview — the campaign
    // already launched into RUNNING, so jump straight to its detail page.
    if (result?.auto_launched) {
      navigate(`/campaigns/${campaignId}`);
      return;
    }
    setStep(4);  // → Research / preview
  }

  function onResearchComplete() {
    if (campaignId) navigate(`/campaigns/${campaignId}/preview`);
  }

  // Last step covers research+compose, AI-compose-only, or template-render
  // depending on the campaign's mode, so keep the chip label mode-agnostic.
  const STEP_LABELS = ['Details', 'Sequence', 'Upload leads', 'Prepare'];

  // The Sequence step needs the full viewport for the canvas; every other
  // step uses the standard narrow wizard width.
  const wide = step === 2;

  return (
    <div className={wide ? 'p-6 w-full' : 'p-8 max-w-2xl mx-auto'}>
      <h1 className="text-2xl font-bold text-slate-900 mb-6">New campaign</h1>

      {/* Step indicator */}
      <div role="list" aria-label="Steps" className="flex items-center gap-2 mb-8 flex-wrap">
        {STEP_LABELS.map((label, i) => {
          const idx = i + 1;
          const active = idx === step;
          const done = idx < step;
          return (
            <div key={label} className="flex items-center gap-2">
              <div
                data-testid={`step-${idx}-indicator`}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium ${
                  active
                    ? 'bg-brand-600 text-white'
                    : done
                    ? 'bg-emerald-100 text-emerald-700'
                    : 'bg-slate-100 text-slate-500'
                }`}
              >
                <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${
                  active ? 'bg-white/20' : done ? 'bg-emerald-600 text-white' : 'bg-slate-300 text-slate-600'
                }`}>
                  {done ? '✓' : idx}
                </span>
                {label}
              </div>
              {i < STEP_LABELS.length - 1 && (
                <div className="w-8 h-px bg-slate-300" />
              )}
            </div>
          );
        })}
      </div>

      {step === 1 && (
        <Step1
          form={form}
          setForm={setForm}
          onSubmit={handleStep1Submit}
          submitting={createMutation.isPending}
          error={error}
          accounts={accounts}
          linkedinAccounts={linkedinAccounts}
          allCampaigns={allCampaigns}
          onConnectInbox={() => setShowInboxModal(true)}
        />
      )}
      {step === 2 && campaignId && (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
          <EmbeddedSequenceBuilder
            campaignId={campaignId}
            onContinue={onSequenceDone}
            onSkip={onSequenceDone}
          />
        </div>
      )}
      {step === 3 && campaignId && (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
          <LeadUpload campaignId={campaignId} onComplete={onUploadComplete} />
        </div>
      )}
      {step === 4 && campaignId && (
        <Step3 campaignId={campaignId} onComplete={onResearchComplete} />
      )}

      {showInboxModal && (
        <ConnectInboxModal
          account={null}
          onClose={() => setShowInboxModal(false)}
          onSaved={(saved) => {
            queryClient.invalidateQueries({ queryKey: ['connected-accounts'] });
            // Auto-select the freshly created account.
            setForm((f) => ({ ...f, connected_account_id: saved.id }));
            setShowInboxModal(false);
          }}
        />
      )}
    </div>
  );
}
