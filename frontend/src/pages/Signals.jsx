import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { convertLead } from '../api/crm.js';
import {
  actionSignal,
  addSignalsToCampaign,
  createWatch,
  createWatchesBulk,
  deleteWatch,
  dismissSignal,
  draftSignalEmail,
  enrichSignalContact,
  listSignals,
  listWatches,
  runWatchNow,
  sendSignalEmail,
  updateWatch,
} from '../api/signals.js';
import { listAccounts as listConnectedAccounts } from '../api/connectedAccounts.js';
import { listCampaigns } from '../api/campaigns.js';
import { signatureToPreviewHtml } from '../utils/signaturePreview.js';
import { useToast } from '../components/Toast.jsx';

const TYPE_BADGES = {
  job_change: { label: 'Job change', cls: 'bg-violet-100 text-violet-700' },
  funding: { label: 'Funding', cls: 'bg-emerald-100 text-emerald-700' },
  hiring: { label: 'Hiring', cls: 'bg-sky-100 text-sky-700' },
  custom: { label: 'Custom', cls: 'bg-slate-100 text-slate-600' },
};

function TypeBadge({ type }) {
  const meta = TYPE_BADGES[type] || TYPE_BADGES.custom;
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${meta.cls}`}>
      {meta.label}
    </span>
  );
}

const SOURCE_BADGES = {
  usaspending: { label: 'USASpending', cls: 'bg-indigo-100 text-indigo-700' },
  irs_bmf: { label: 'IRS BMF', cls: 'bg-teal-100 text-teal-700' },
};

function SourceBadge({ source }) {
  // No source = a watch-sourced signal.
  const meta = SOURCE_BADGES[source] || { label: 'Watch', cls: 'bg-slate-100 text-slate-500' };
  return (
    <span
      data-testid="signal-source-badge"
      className={`px-2 py-0.5 rounded-full text-xs font-semibold ${meta.cls}`}
    >
      {meta.label}
    </span>
  );
}

function SignalCard({ signal, onOpen, checked, onToggle }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });

  const actionMut = useMutation({
    mutationFn: () => actionSignal(signal.id),
    onSuccess: () => { invalidate(); toast.success('Marked actioned'); },
  });
  const dismissMut = useMutation({
    mutationFn: () => dismissSignal(signal.id),
    onSuccess: () => { invalidate(); toast.success('Dismissed'); },
  });
  const enrichMut = useMutation({
    mutationFn: () => enrichSignalContact(signal.id),
    onSuccess: (d) => {
      if (d.found) {
        invalidate();
        const lead = d.lead_created ? 'Lead created' : 'Lead updated';
        toast.success(
          d.email
            ? `${lead} — ${d.generic ? 'general inbox' : 'contact'}: ${d.email}`
            : `${lead} from LinkedIn profile (no email found)`,
        );
      } else {
        toast.info('No contact could be found for this org.');
      }
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Lookup failed'),
  });
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };

  const detail = signal.detail || {};
  return (
    <div
      data-testid={`signal-card-${signal.id}`}
      onClick={() => onOpen(signal)}
      className="bg-white border border-slate-200 rounded-xl p-4 flex items-start gap-3 cursor-pointer hover:border-brand-300 hover:shadow-sm transition"
    >
      {signal.status === 'new' && onToggle && (
        <input
          type="checkbox"
          data-testid={`select-signal-${signal.id}`}
          checked={!!checked}
          onClick={(e) => e.stopPropagation()}
          onChange={() => onToggle(signal.id)}
          className="mt-1 shrink-0"
          aria-label="Select signal"
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <TypeBadge type={signal.signal_type} />
          <SourceBadge source={signal.source} />
          <span className="text-xs text-slate-400">
            {new Date(signal.detected_at).toLocaleString()}
          </span>
          {signal.status !== 'new' && (
            <span className="text-xs text-slate-400 italic">{signal.status}</span>
          )}
        </div>
        <p className="font-medium text-slate-800 m-0">{signal.summary}</p>
        <p className="text-xs text-slate-500 m-0 mt-1">
          {detail.old_title && `${detail.old_title} → ${detail.new_title}`}
          {detail.amount && ` · ${detail.amount}`}
          {(detail.roles || []).length > 0 && `Roles: ${detail.roles.join(', ')}`}
          {detail.source_url && (
            <>
              {' · '}
              <a
                href={detail.source_url} target="_blank" rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-brand-600"
              >
                source ↗
              </a>
            </>
          )}
        </p>
      </div>
      {signal.status === 'new' && (
        <div className="flex gap-2 shrink-0">
          {signal.lead_id && signal.lead_has_email ? (
            <button
              type="button"
              data-testid={`draft-signal-${signal.id}`}
              onClick={stop(() => onOpen(signal))}
              className="text-sm px-3 py-1.5 rounded-lg bg-brand-600 text-white hover:bg-brand-700"
            >
              Draft &amp; send
            </button>
          ) : signal.lead_id ? (
            <button
              type="button"
              data-testid={`view-lead-signal-${signal.id}`}
              onClick={stop(() => onOpen(signal))}
              className="text-sm px-3 py-1.5 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50"
            >
              View lead
            </button>
          ) : (
            <button
              type="button"
              data-testid={`enrich-signal-${signal.id}`}
              disabled={enrichMut.isPending}
              onClick={stop(() => enrichMut.mutate())}
              className="text-sm px-3 py-1.5 rounded-lg border border-brand-300 text-brand-700 hover:bg-brand-50 disabled:opacity-50"
            >
              {enrichMut.isPending ? 'Searching…' : 'Find contact'}
            </button>
          )}
          <button
            type="button"
            data-testid={`action-signal-${signal.id}`}
            onClick={stop(() => actionMut.mutate())}
            className="text-sm px-3 py-1.5 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50"
          >
            Actioned
          </button>
          <button
            type="button"
            data-testid={`dismiss-signal-${signal.id}`}
            onClick={stop(() => dismissMut.mutate())}
            className="text-sm px-3 py-1.5 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50"
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}


function SignalDetailModal({ signal, onClose }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [stage, setStage] = useState('idle');   // idle | editing | sent
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [toEmail, setToEmail] = useState('');
  const [fromEmail, setFromEmail] = useState('');   // '' = workspace default
  const [crmNote, setCrmNote] = useState(null);
  // Draft-configuration: a free-text prompt steering the AI + an optional tone.
  const [promptGoal, setPromptGoal] = useState('');
  const [promptTone, setPromptTone] = useState('');
  // Per-send signature override: null = use the sender account's signature.
  const [sigOverride, setSigOverride] = useState(null);
  const [enrichedLeadId, setEnrichedLeadId] = useState(null);
  const [enrichedEmailable, setEnrichedEmailable] = useState(false);
  const hasLead = !!(signal.lead_id || enrichedLeadId);
  // Whether the linked lead can actually be emailed (drives the draft flow).
  const emailable = enrichedLeadId
    ? enrichedEmailable
    : !!(signal.lead_id && signal.lead_has_email);

  const { data: accounts = [] } = useQuery({
    queryKey: ['connected-accounts'],
    queryFn: listConnectedAccounts,
  });
  const resolvedAccount = fromEmail
    ? accounts.find((a) => a.email_address === fromEmail)
    : accounts.find((a) => a.is_default_sender);
  const accountSignature = resolvedAccount?.signature || '';
  // Effective signature shown/sent: the override if the user edited it,
  // otherwise the resolved account's signature.
  const effectiveSignature = sigOverride === null ? accountSignature : sigOverride;

  const detail = signal.detail || {};

  const draftMut = useMutation({
    mutationFn: () => draftSignalEmail(signal.id, {
      ...(promptGoal.trim() ? { goal: promptGoal.trim() } : {}),
      ...(promptTone.trim() ? { tone: promptTone.trim() } : {}),
    }),
    onSuccess: (d) => {
      setToEmail(d.to_email);
      setSubject(d.subject);
      setBody(d.body);
      setStage('editing');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Draft failed'),
  });

  const sendMut = useMutation({
    mutationFn: () => sendSignalEmail(signal.id, {
      subject,
      body,
      ...(fromEmail ? { sender_email: fromEmail } : {}),
      // Only send a signature override when the user actually edited it;
      // otherwise the backend uses the sender account's signature.
      ...(sigOverride === null ? {} : { signature: sigOverride }),
    }),
    onSuccess: (d) => {
      setStage('sent');
      if (d.crm_activity_logged) {
        setCrmNote(
          d.crm_lead_created
            ? 'Logged as a new CRM lead + outbound email activity.'
            : 'Logged as an outbound email activity on the lead.',
        );
      }
      queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });
      toast.success('Sent — signal marked actioned');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Send failed'),
  });

  const [foundLinkedin, setFoundLinkedin] = useState(null);
  const enrichMut = useMutation({
    mutationFn: () => enrichSignalContact(signal.id),
    onSuccess: (d) => {
      if (d.linkedin_url) setFoundLinkedin(d.linkedin_url);
      if (d.found && d.lead_id) {
        setEnrichedLeadId(d.lead_id);
        setEnrichedEmailable(!!d.has_email);
        queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });
        const lead = d.lead_created ? 'Lead created' : 'Lead updated';
        toast.success(
          d.email
            ? `${lead} — ${d.generic ? 'general inbox' : 'contact'}: ${d.email}`
            : `${lead} from LinkedIn profile (no email found)`,
        );
      } else {
        toast.info('No contact could be found for this org.');
      }
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Lookup failed'),
  });

  // Lead-level actions (Create opportunity / Add to campaign) — available
  // whenever the signal has a staged lead, regardless of the email stage.
  const leadId = signal.lead_id || enrichedLeadId;
  const [campaignId, setCampaignId] = useState('');
  const { data: campaigns = [] } = useQuery({
    queryKey: ['campaigns'],
    queryFn: listCampaigns,
  });
  const addableCampaigns = campaigns.filter((c) => c.status !== 'complete');

  const convertMut = useMutation({
    mutationFn: () => convertLead(leadId),
    onSuccess: (d) => {
      queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });
      toast.success('Opportunity created');
      if (d?.opportunity_id) navigate(`/opportunities/${d.opportunity_id}`);
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Could not create opportunity'),
  });

  const addToCampaignMut = useMutation({
    mutationFn: () => addSignalsToCampaign({ signal_ids: [signal.id], campaign_id: campaignId }),
    onSuccess: (d) => {
      queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });
      if (d.added > 0) {
        toast.success('Added to campaign — emails will run automatically');
        onClose();
      } else {
        toast.info('Not added (already in the campaign, suppressed, or no contact).');
      }
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Add to campaign failed'),
  });

  return (
    <div
      data-testid="signal-detail-modal"
      className="fixed inset-0 z-50 bg-black/40 flex items-start justify-center p-4 overflow-auto"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-xl max-w-2xl w-full mt-10 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <TypeBadge type={signal.signal_type} />
              <SourceBadge source={signal.source} />
            </div>
            <h2 className="text-lg font-semibold text-slate-900 m-0">{signal.summary}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-xl leading-none"
            aria-label="Close"
          >×</button>
        </div>

        {/* Signal detail */}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm mb-4">
          {Object.entries(detail).filter(([k]) => k !== '_cost_usd').map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-500 capitalize">{k.replace(/_/g, ' ')}</dt>
              <dd className="text-slate-800 m-0 truncate">
                {Array.isArray(v) ? v.join(', ') : String(v ?? '—')}
              </dd>
            </div>
          ))}
        </dl>

        {/* Contact & links — what enrichment found for this org. */}
        {(() => {
          const lead = signal.lead || {};
          const website = lead.company_website || detail.website || detail.domain;
          const contactName = [lead.first_name, lead.last_name].filter(Boolean).join(' ');
          const href = website
            ? (/^https?:\/\//i.test(website) ? website : `https://${website}`)
            : null;
          if (!website && !lead.email && !contactName && !lead.linkedin_url) return null;
          return (
            <div
              data-testid="signal-contact-info"
              className="mb-4 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm space-y-1"
            >
              <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Contact &amp; links
              </div>
              {(contactName || lead.job_title) && (
                <div className="text-slate-800">
                  {contactName || '—'}
                  {lead.job_title ? <span className="text-slate-500">, {lead.job_title}</span> : null}
                </div>
              )}
              {lead.email && (
                <div>
                  ✉{' '}
                  <a className="text-brand-600 hover:underline" href={`mailto:${lead.email}`}>
                    {lead.email}
                  </a>
                </div>
              )}
              {href && (
                <div>
                  🌐{' '}
                  <a
                    data-testid="signal-website-link"
                    href={href} target="_blank" rel="noreferrer"
                    className="text-brand-600 hover:underline break-all"
                  >
                    {website}
                  </a>
                </div>
              )}
              {lead.linkedin_url && (
                <div>
                  in{' '}
                  <a
                    data-testid="signal-linkedin-link"
                    href={lead.linkedin_url} target="_blank" rel="noreferrer"
                    className="text-brand-600 hover:underline break-all"
                  >
                    LinkedIn profile
                  </a>
                </div>
              )}
            </div>
          );
        })()}

        {!hasLead ? (
          <div data-testid="signal-no-contact" className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            <p className="m-0">
              No contact email was found for this org yet (notification-only).
              Try a light contact lookup, or mark it actioned / dismiss it.
            </p>
            <button
              type="button"
              data-testid="signal-enrich-btn"
              disabled={enrichMut.isPending}
              onClick={() => enrichMut.mutate()}
              className="mt-2 px-3 py-1.5 text-sm font-medium rounded-lg border border-brand-300 text-brand-700 bg-white hover:bg-brand-50 disabled:opacity-50"
            >
              {enrichMut.isPending ? 'Searching…' : '🔎 Find contact (web + LinkedIn)'}
            </button>
          </div>
        ) : !emailable ? (
          <div data-testid="signal-linkedin-only" className="text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
            <p className="m-0 font-medium">
              Lead created — LinkedIn only (no email address found).
            </p>
            <p className="m-0 mt-1 text-slate-500">
              Reach out on LinkedIn; there's nothing to email yet.
            </p>
            {foundLinkedin && (
              <a
                data-testid="signal-found-linkedin"
                href={foundLinkedin} target="_blank" rel="noreferrer"
                className="inline-block mt-2 text-brand-600 underline"
              >
                {foundLinkedin}
              </a>
            )}
          </div>
        ) : stage === 'sent' ? (
          <div data-testid="signal-send-success" className="text-sm">
            <p className="text-emerald-700 font-medium m-0">Sent to {toEmail}.</p>
            {crmNote && (
              <p data-testid="signal-crm-note" className="text-slate-500 mt-1 m-0">{crmNote}</p>
            )}
            <button
              type="button"
              onClick={onClose}
              className="mt-3 px-3 py-1.5 text-sm border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50"
            >
              Done
            </button>
          </div>
        ) : stage === 'idle' ? (
          <div className="space-y-3">
            <label className="block text-sm">
              <span className="text-slate-600">Prompt — what should this email focus on? (optional)</span>
              <textarea
                data-testid="signal-prompt"
                value={promptGoal}
                onChange={(e) => setPromptGoal(e.target.value)}
                rows={2}
                placeholder="e.g. Congratulate them on the grant and offer a 15-min call about grant-writing AI"
                className="mt-1 block w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-slate-600">Tone (optional)</span>
                <input
                  type="text"
                  data-testid="signal-tone"
                  value={promptTone}
                  onChange={(e) => setPromptTone(e.target.value)}
                  placeholder="warm and professional"
                  className="mt-1 block w-full border border-slate-300 rounded-lg px-2 py-1.5 text-sm"
                />
              </label>
              <label className="block text-sm">
                <span className="text-slate-600">Send from</span>
                <select
                  data-testid="signal-from-picker"
                  value={fromEmail}
                  onChange={(e) => setFromEmail(e.target.value)}
                  className="mt-1 block w-full border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
                >
                  <option value="">Workspace default sender</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.email_address}>
                      {a.label} ({a.email_address})
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <button
              type="button"
              data-testid="signal-draft-btn"
              onClick={() => draftMut.mutate()}
              disabled={draftMut.isPending}
              className="px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg disabled:opacity-50"
            >
              {draftMut.isPending ? 'Drafting…' : '✦ Draft email'}
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="text-sm text-slate-600" data-testid="signal-to">
              To: <span className="font-medium text-slate-800">{toEmail}</span>
            </div>
            <label className="block text-sm">
              <span className="text-slate-600">Send from</span>
              <select
                data-testid="signal-from-picker"
                value={fromEmail}
                onChange={(e) => setFromEmail(e.target.value)}
                className="mt-1 block w-full border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
              >
                <option value="">Workspace default sender</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.email_address}>
                    {a.label} ({a.email_address})
                  </option>
                ))}
              </select>
            </label>
            <input
              type="text"
              data-testid="signal-subject"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              className="block w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-medium"
            />
            <textarea
              data-testid="signal-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={9}
              className="block w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
            />
            <label className="block text-sm">
              <span className="text-slate-600">
                Signature {sigOverride === null ? '(from your account — editable)' : '(custom for this send)'}
              </span>
              <textarea
                data-testid="signal-signature"
                value={effectiveSignature}
                onChange={(e) => setSigOverride(e.target.value)}
                rows={4}
                placeholder="No signature — add one here, or set a default on the account in Settings."
                className="mt-1 block w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-[inherit]"
              />
              {sigOverride !== null && (
                <button
                  type="button"
                  data-testid="signal-signature-reset"
                  onClick={() => setSigOverride(null)}
                  className="mt-1 text-xs text-brand-600 hover:underline"
                >
                  Reset to account signature
                </button>
              )}
            </label>
            {effectiveSignature.trim() && (
              <div className="text-xs text-slate-400">
                <span className="block mb-1">Preview:</span>
                <div
                  data-testid="signal-signature-preview"
                  className="border border-slate-200 rounded-lg p-2 text-slate-600"
                  dangerouslySetInnerHTML={{ __html: signatureToPreviewHtml(effectiveSignature.trim()) }}
                />
              </div>
            )}
            <div className="flex justify-end">
              <button
                type="button"
                data-testid="signal-send-btn"
                onClick={() => sendMut.mutate()}
                disabled={sendMut.isPending || !subject.trim() || !body.trim()}
                className="px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg disabled:opacity-50"
              >
                {sendMut.isPending ? 'Sending…' : 'Send email'}
              </button>
            </div>
          </div>
        )}

        {/* Lead-level actions — push the staged lead into the CRM as an
            opportunity, or into a sending campaign.  Shown whenever there's
            a lead (email or LinkedIn-only). */}
        {leadId && (
          <div
            data-testid="signal-lead-actions"
            className="mt-4 pt-4 border-t border-slate-200 flex flex-wrap items-center gap-2"
          >
            <button
              type="button"
              data-testid="signal-create-opp-btn"
              onClick={() => convertMut.mutate()}
              disabled={convertMut.isPending}
              className="text-sm px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {convertMut.isPending ? 'Creating…' : '＋ Create opportunity'}
            </button>
            <div className="flex items-center gap-2 ml-auto">
              <select
                data-testid="signal-add-campaign-select"
                value={campaignId}
                onChange={(e) => setCampaignId(e.target.value)}
                className="border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
              >
                <option value="">Add to campaign…</option>
                {addableCampaigns.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
              <button
                type="button"
                data-testid="signal-add-campaign-btn"
                onClick={() => addToCampaignMut.mutate()}
                disabled={!campaignId || addToCampaignMut.isPending}
                className="text-sm px-3 py-1.5 rounded-lg bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
              >
                {addToCampaignMut.isPending ? 'Adding…' : 'Add'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const WATCH_TYPE_META = {
  job_change: {
    label: 'Job change',
    desc: "Alerts when this person's job title changes. Matched by EMAIL — that's the one required field.",
    needsEmail: true,
  },
  funding: {
    label: 'Funding',
    desc: 'Alerts when a company announces a new funding round or stage. Needs a COMPANY name. Supports pasting multiple companies.',
    needsCompany: true,
    bulk: true,
  },
  hiring: {
    label: 'Hiring',
    desc: 'Alerts when a company starts hiring growth roles (sales, engineering, ops leadership). Needs a COMPANY name. Supports pasting multiple companies.',
    needsCompany: true,
    bulk: true,
  },
  custom: {
    label: 'Everything',
    desc: 'Runs every check it can: job change (needs email), funding + hiring (need company). Give at least one of email / company.',
    needsEither: true,
  },
};

function NewWatchForm({ onDone }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    watch_type: 'funding', company: '', person_name: '', email: '',
    frequency: 'daily',
  });
  const [bulkMode, setBulkMode] = useState(false);
  const [bulkCompanies, setBulkCompanies] = useState('');

  const meta = WATCH_TYPE_META[form.watch_type];
  const bulkAvailable = !!meta.bulk;
  const bulkList = bulkCompanies.split('\n').map((x) => x.trim()).filter(Boolean);

  const canSave = (() => {
    if (bulkMode && bulkAvailable) return bulkList.length > 0;
    if (meta.needsEmail) return !!form.email.trim();
    if (meta.needsCompany) return !!form.company.trim();
    if (meta.needsEither) return !!(form.email.trim() || form.company.trim());
    return false;
  })();

  const onSuccess = (msg) => {
    queryClient.invalidateQueries({ queryKey: ['signal-watches'] });
    toast.success(msg);
    onDone();
  };

  const createMut = useMutation({
    mutationFn: () => createWatch({
      watch_type: form.watch_type,
      frequency: form.frequency,
      company: form.company.trim() || null,
      person_name: form.person_name.trim() || null,
      email: form.email.trim() || null,
    }),
    onSuccess: () => onSuccess('Watch created — first check runs within a minute'),
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to create watch'),
  });

  const bulkMut = useMutation({
    mutationFn: () => createWatchesBulk({
      watch_type: form.watch_type,
      frequency: form.frequency,
      companies: bulkList,
    }),
    onSuccess: (res) => onSuccess(
      `${res.created} watch${res.created === 1 ? '' : 'es'} created`
      + (res.skipped_duplicate ? ` · ${res.skipped_duplicate} already watched` : ''),
    ),
    onError: (err) => toast.error(err?.response?.data?.detail || 'Bulk create failed'),
  });

  const pending = createMut.isPending || bulkMut.isPending;
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <div data-testid="new-watch-form" className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3 mb-4">
      {/* Type picker with descriptions */}
      <div className="flex flex-wrap gap-1.5">
        {Object.entries(WATCH_TYPE_META).map(([value, m]) => (
          <button
            key={value}
            type="button"
            data-testid={`watch-type-${value}`}
            aria-pressed={form.watch_type === value}
            onClick={() => { setForm({ ...form, watch_type: value }); setBulkMode(false); }}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg border ${
              form.watch_type === value
                ? 'bg-brand-600 text-white border-brand-600'
                : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-100'
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>
      <p data-testid="watch-type-desc" className="text-xs text-slate-500 m-0">{meta.desc}</p>

      {bulkAvailable && (
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            data-testid="bulk-mode-toggle"
            checked={bulkMode}
            onChange={(e) => setBulkMode(e.target.checked)}
          />
          Multiple companies (one watch each)
        </label>
      )}

      {bulkMode && bulkAvailable ? (
        <div>
          <textarea
            data-testid="bulk-companies-input"
            rows={5}
            value={bulkCompanies}
            onChange={(e) => setBulkCompanies(e.target.value)}
            placeholder={'Acme Corp\nBeta Inc\nGamma LLC'}
            className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white font-mono"
          />
          <p className="text-xs text-slate-400 m-0 mt-1">
            One company per line · {bulkList.length} compan{bulkList.length === 1 ? 'y' : 'ies'} —
            companies already watched for this signal are skipped.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-3">
          {(meta.needsEmail || meta.needsEither) && (
            <>
              <input
                data-testid="watch-person"
                placeholder="Person name (optional)"
                value={form.person_name}
                onChange={set('person_name')}
                className="border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
              />
              <input
                data-testid="watch-email"
                placeholder={meta.needsEmail ? 'Email (required)' : 'Email'}
                value={form.email}
                onChange={set('email')}
                className="border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
              />
            </>
          )}
          <input
            data-testid="watch-company"
            placeholder={meta.needsCompany ? 'Company (required)' : 'Company'}
            value={form.company}
            onChange={set('company')}
            className="border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
          />
        </div>
      )}

      <div className="flex items-center gap-3">
        <label className="text-xs text-slate-600">
          Check
          <select
            value={form.frequency}
            onChange={set('frequency')}
            className="ml-2 border border-slate-300 rounded-lg px-2 py-1.5 text-sm bg-white"
          >
            <option value="every_6h">Every 6h</option>
            <option value="every_12h">Every 12h</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="manual">Manual only</option>
          </select>
        </label>
        <span className="text-xs text-slate-400">
          Already in your pipeline? Track a lead from its record on the Leads page instead.
        </span>
      </div>

      <div className="flex justify-end gap-2">
        <button type="button" onClick={onDone} className="px-3 py-1.5 text-sm border border-slate-300 rounded-lg text-slate-700">Cancel</button>
        <button
          type="button"
          data-testid="save-watch-btn"
          onClick={() => (bulkMode && bulkAvailable ? bulkMut.mutate() : createMut.mutate())}
          disabled={pending || !canSave}
          className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg disabled:opacity-50"
        >
          {pending ? 'Saving…' : bulkMode && bulkAvailable
            ? `Create ${bulkList.length || ''} watch${bulkList.length === 1 ? '' : 'es'}`
            : 'Create watch'}
        </button>
      </div>
    </div>
  );
}

function WatchesTab() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const { data: watches = [] } = useQuery({
    queryKey: ['signal-watches'],
    queryFn: listWatches,
  });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['signal-watches'] });

  const runMut = useMutation({
    mutationFn: runWatchNow,
    onSuccess: () => toast.success('Check enqueued'),
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const pauseMut = useMutation({
    mutationFn: ({ id, status }) => updateWatch(id, { status }),
    onSuccess: invalidate,
  });
  const deleteMut = useMutation({
    mutationFn: deleteWatch,
    onSuccess: () => { invalidate(); toast.success('Watch deleted'); },
  });

  return (
    <div>
      <div className="flex justify-end mb-3">
        <button
          type="button"
          data-testid="new-watch-btn"
          onClick={() => setShowForm((v) => !v)}
          className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg"
        >
          {showForm ? 'Cancel' : '+ New watch'}
        </button>
      </div>
      {showForm && <NewWatchForm onDone={() => setShowForm(false)} />}
      {watches.length === 0 && !showForm && (
        <p data-testid="watches-empty" className="text-sm text-slate-400 text-center py-8">
          No watches yet. Create one to monitor a person's job changes (by
          email) or a company's funding/hiring — or paste a whole list of
          companies with the "Multiple companies" option.
        </p>
      )}
      <div className="space-y-2">
        {watches.map((w) => (
          <div key={w.id} data-testid={`watch-row-${w.id}`} className="bg-white border border-slate-200 rounded-xl p-4 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <TypeBadge type={w.watch_type} />
                <span className="font-medium text-slate-800 truncate">
                  {w.person_name || w.company || w.email
                    || (w.lead_id ? 'Tracked lead' : w.opportunity_id ? 'Tracked deal' : 'Tracked record')}
                </span>
                {w.person_name && w.company && (
                  <span className="text-sm text-slate-500">· {w.company}</span>
                )}
                {(w.lead_id || w.opportunity_id) && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-indigo-100 text-indigo-700">
                    {w.lead_id ? 'CRM lead' : 'deal'}
                  </span>
                )}
                {w.status === 'paused' && (
                  <span className="text-xs text-amber-600">paused</span>
                )}
              </div>
              <p className="text-xs text-slate-400 m-0 mt-0.5" data-testid={`watch-baseline-${w.id}`}>
                {w.frequency} · last run: {w.last_run_at ? new Date(w.last_run_at).toLocaleString() : 'never (first check within a minute)'}
                {w.last_run_status ? ` (${w.last_run_status})` : ''}
                {w.last_seen?.job_title && ` · baseline title: ${w.last_seen.job_title}`}
                {w.last_seen?.funding_stage && ` · baseline stage: ${w.last_seen.funding_stage}`}
                {(w.last_seen?.hiring_roles || []).length > 0
                  && ` · tracking ${w.last_seen.hiring_roles.length} open role${w.last_seen.hiring_roles.length === 1 ? '' : 's'}`}
              </p>
            </div>
            <div className="flex gap-2 shrink-0 text-sm">
              <button type="button" onClick={() => runMut.mutate(w.id)} className="text-brand-600 hover:text-brand-800">Run now</button>
              <button
                type="button"
                onClick={() => pauseMut.mutate({ id: w.id, status: w.status === 'active' ? 'paused' : 'active' })}
                className="text-slate-500 hover:text-slate-700"
              >
                {w.status === 'active' ? 'Pause' : 'Resume'}
              </button>
              <button type="button" onClick={() => deleteMut.mutate(w.id)} className="text-red-500 hover:text-red-700">Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Signals() {
  const [tab, setTab] = useState('feed');
  const [statusFilter, setStatusFilter] = useState('new');
  const [sourceFilter, setSourceFilter] = useState('');
  const [selected, setSelected] = useState(null);
  const [checked, setChecked] = useState(() => new Set());
  const [targetCampaignId, setTargetCampaignId] = useState('');
  const toast = useToast();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['prospect-signals', statusFilter, sourceFilter],
    queryFn: () => listSignals({
      ...(statusFilter ? { status: statusFilter } : {}),
      ...(sourceFilter ? { source: sourceFilter } : {}),
    }),
  });
  const items = data?.items || [];

  const { data: campaigns = [] } = useQuery({
    queryKey: ['campaigns'],
    queryFn: listCampaigns,
  });
  const addableCampaigns = campaigns.filter((c) => c.status !== 'complete');

  const toggleChecked = (id) => setChecked((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  const addMutation = useMutation({
    mutationFn: () => addSignalsToCampaign({
      signal_ids: [...checked],
      campaign_id: targetCampaignId,
    }),
    onSuccess: (res) => {
      const bits = [`${res.added} added`];
      if (res.skipped_duplicate) bits.push(`${res.skipped_duplicate} already in campaign`);
      if (res.skipped_suppressed) bits.push(`${res.skipped_suppressed} suppressed`);
      if (res.skipped_no_contact) bits.push(`${res.skipped_no_contact} without a contact`);
      toast.success(
        bits.join(' · ')
        + (res.research_started ? ' — emails will run automatically' : res.added ? ' — will run at launch' : ''),
      );
      setChecked(new Set());
      setTargetCampaignId('');
      queryClient.invalidateQueries({ queryKey: ['prospect-signals'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Add to campaign failed'),
  });

  return (
    <div data-testid="signals-page" className="p-8 max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 mb-1">Signals</h1>
      <p className="text-slate-500 text-sm mt-0 mb-4">
        Job changes, funding rounds, and hiring sprees on tracked prospects —
        reach out while the trigger is fresh.
      </p>

      <details data-testid="signals-help" className="bg-brand-50 border border-brand-200 rounded-xl px-4 py-3 mb-4 text-sm text-brand-900">
        <summary className="font-medium cursor-pointer">How signals work</summary>
        <div className="mt-2 space-y-2 text-brand-900/90">
          <p className="m-0">
            A <strong>watch</strong> monitors one target on a schedule. When
            something changes, a <strong>signal</strong> lands in this feed —
            plus an alert, and a "Reach out" task when the target is a
            tracked lead or deal. Cold targets with an email get staged as a
            CRM lead. Nothing is ever emailed or added to a campaign
            automatically.
          </p>
          <ul className="m-0 pl-5">
            <li><strong>Job change</strong> — a person's title changed (needs their <em>email</em>; uses Apollo).</li>
            <li><strong>Funding</strong> — a company announced a new round/stage (needs the <em>company name</em>).</li>
            <li><strong>Hiring</strong> — a company is hiring growth roles (needs the <em>company name</em>).</li>
            <li><strong>Everything</strong> — all of the above, using whatever fields you give it.</li>
          </ul>
          <p className="m-0">
            Each watch tracks <strong>one</strong> company or person — to
            monitor a list of companies, use the "Multiple companies" option
            when creating a funding or hiring watch. The first check seeds a
            baseline; signals fire on <em>changes</em> after that, once each.
          </p>
        </div>
      </details>
      <div role="tablist" className="flex border-b border-slate-200 mb-4">
        {[['feed', 'Feed'], ['watches', 'Watches']].map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px bg-transparent cursor-pointer ${
              tab === key ? 'border-brand-600 text-brand-600' : 'border-transparent text-slate-500'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'feed' && (
        <div>
          <div className="flex justify-end gap-2 mb-3">
            <select
              data-testid="signal-source-filter"
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="border border-slate-300 rounded-lg px-3 py-1.5 text-sm bg-white"
            >
              <option value="">All sources</option>
              <option value="watch">Watches</option>
              <option value="usaspending">USASpending</option>
              <option value="irs_bmf">IRS BMF</option>
            </select>
            <select
              data-testid="signal-status-filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="border border-slate-300 rounded-lg px-3 py-1.5 text-sm bg-white"
            >
              <option value="new">New</option>
              <option value="actioned">Actioned</option>
              <option value="dismissed">Dismissed</option>
              <option value="">All</option>
            </select>
          </div>
          {checked.size > 0 && (
            <div
              data-testid="signal-add-to-campaign-bar"
              className="bg-brand-50 border border-brand-200 rounded-xl p-3 mb-3 flex flex-wrap items-center gap-3"
            >
              <span className="text-sm font-medium text-brand-900">
                {checked.size} signal{checked.size > 1 ? 's' : ''} selected
              </span>
              <select
                data-testid="signal-target-campaign"
                value={targetCampaignId}
                onChange={(e) => setTargetCampaignId(e.target.value)}
                className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white"
              >
                <option value="">Pick a campaign…</option>
                {addableCampaigns.map((c) => (
                  <option key={c.id} value={c.id}>{c.name} ({c.status})</option>
                ))}
              </select>
              <button
                type="button"
                data-testid="signal-add-to-campaign-btn"
                onClick={() => addMutation.mutate()}
                disabled={!targetCampaignId || addMutation.isPending}
                className="px-4 py-1.5 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg disabled:opacity-50"
              >
                {addMutation.isPending ? 'Adding…' : 'Add to campaign'}
              </button>
              <button
                type="button"
                onClick={() => setChecked(new Set())}
                className="text-sm text-slate-500 hover:text-slate-700"
              >
                Clear
              </button>
              <span className="text-xs text-brand-700/70 basis-full">
                Their staged leads are copied into the campaign and run the
                normal research → compose → send pipeline. Signals without a
                contact are skipped.
              </span>
            </div>
          )}
          {items.length === 0 && (
            <p data-testid="signals-empty" className="text-sm text-slate-400 text-center py-8">
              No signals here. Watches run on their schedule and detected
              events land in this feed.
            </p>
          )}
          <div className="space-y-2">
            {items.map((s) => (
              <SignalCard
                key={s.id}
                signal={s}
                onOpen={setSelected}
                checked={checked.has(s.id)}
                onToggle={toggleChecked}
              />
            ))}
          </div>
        </div>
      )}
      {tab === 'watches' && <WatchesTab />}
      {selected && (
        <SignalDetailModal signal={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
