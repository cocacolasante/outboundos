import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  addLeadsToCampaign,
  getLeadById,
  ignoreLead,
  listAllLeads,
  listCampaigns,
  unignoreLead,
  updateLeadEmail,
} from '../api/campaigns.js';
import { useToast } from '../components/Toast.jsx';
import ActivityLog from '../components/ActivityLog.jsx';
import { Skeleton, EmptyState, ErrorState } from '../components/states.jsx';
import { Field, Input, Textarea, Button } from '../components/ui.jsx';
import {
  convertLead,
  createCrmLead,
  updateLeadCrmStatus,
  updateLeadFields,
} from '../api/crm.js';
import { createWatch } from '../api/signals.js';

const STATUS_CLASSES = {
  pending: 'bg-slate-100 text-slate-700',
  scheduled: 'bg-amber-100 text-amber-700',
  sent: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700',
  // Terminal state for ignored / unsubscribed / bounced emails —
  // distinct from failed so the user can tell a deliberate ignore from
  // a delivery problem at a glance.
  suppressed: 'bg-red-50 text-red-600',
};

const CRM_STATUS_CLASSES = {
  new: 'bg-sky-100 text-sky-700',
  working: 'bg-amber-100 text-amber-700',
  qualified: 'bg-emerald-100 text-emerald-700',
  converted: 'bg-violet-100 text-violet-700',
  unqualified: 'bg-slate-200 text-slate-600',
};

// Statuses the user can set directly; ``converted`` only via Convert.
const SETTABLE_CRM_STATUSES = ['new', 'working', 'qualified', 'unqualified'];

function StatusPill({ value }) {
  if (!value) return <span className="text-slate-400">—</span>;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${STATUS_CLASSES[value] || 'bg-slate-100 text-slate-700'}`}>
      {value}
    </span>
  );
}

export default function Leads() {
  const [page, setPage] = useState(1);
  const [campaignId, setCampaignId] = useState('');
  const [search, setSearch] = useState('');
  const [hasNotes, setHasNotes] = useState(false);
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [checked, setChecked] = useState(() => new Set());
  const [targetCampaignId, setTargetCampaignId] = useState('');
  const toast = useToast();
  const queryClient = useQueryClient();

  const { data: campaigns = [] } = useQuery({
    queryKey: ['campaigns'],
    queryFn: listCampaigns,
  });
  // Complete campaigns will never send — not valid add targets.
  const addableCampaigns = campaigns.filter((c) => c.status !== 'complete');

  const toggleChecked = (id) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const addMutation = useMutation({
    mutationFn: () => addLeadsToCampaign(targetCampaignId, [...checked]),
    onSuccess: (res) => {
      const bits = [`${res.added} added`];
      if (res.skipped_duplicate) bits.push(`${res.skipped_duplicate} already in campaign`);
      if (res.skipped_suppressed) bits.push(`${res.skipped_suppressed} suppressed`);
      toast.success(
        bits.join(' · ')
        + (res.research_started ? ' — research started' : res.added ? ' — will process at launch' : ''),
      );
      setChecked(new Set());
      setTargetCampaignId('');
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      queryClient.invalidateQueries({ queryKey: ['campaigns'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Add to campaign failed'),
  });

  const params = { page, page_size: 50 };
  if (campaignId) params.campaign_id = campaignId;
  if (search.trim()) params.search = search.trim();
  if (hasNotes) params.has_notes = true;

  const { data: leadsPage, isLoading, error, refetch } = useQuery({
    queryKey: ['all-leads', params],
    queryFn: () => listAllLeads(params),
    keepPreviousData: true,
  });

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Leads</h1>
        <button
          type="button"
          onClick={() => setCreating(true)}
          data-testid="new-lead-btn"
          className="px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg"
        >
          + New lead
        </button>
      </div>
      <p className="text-sm text-slate-500 mb-5">
        Every lead across every campaign — plus manually-created CRM leads.
        Click a row for the full record: history, activities, notes, convert.
      </p>

      {/* Filters */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4 mb-4 flex flex-wrap items-center gap-3">
        <input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          placeholder="Search email, name, company…"
          aria-label="Search leads"
          className="flex-1 min-w-[200px] px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
        />
        <select
          value={campaignId}
          onChange={(e) => { setCampaignId(e.target.value); setPage(1); }}
          aria-label="Filter by campaign"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white"
        >
          <option value="">All campaigns</option>
          {campaigns.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={hasNotes}
            onChange={(e) => { setHasNotes(e.target.checked); setPage(1); }}
          />
          Has notes
        </label>
      </div>

      {/* Bulk add-to-campaign bar */}
      {checked.size > 0 && (
        <div
          data-testid="add-to-campaign-bar"
          className="bg-brand-50 border border-brand-200 rounded-xl p-3 mb-4 flex flex-wrap items-center gap-3"
        >
          <span className="text-sm font-medium text-brand-900">
            {checked.size} lead{checked.size > 1 ? 's' : ''} selected
          </span>
          <select
            data-testid="target-campaign-select"
            value={targetCampaignId}
            onChange={(e) => setTargetCampaignId(e.target.value)}
            className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white"
          >
            <option value="">Pick a campaign…</option>
            {addableCampaigns.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.status})
              </option>
            ))}
          </select>
          <button
            type="button"
            data-testid="add-to-campaign-btn"
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
            Leads are copied in — the original record (and its CRM history)
            stays put. Suppressed emails and duplicates are skipped.
          </span>
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <table className="w-full text-sm" data-testid="leads-table">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase">
              <th className="px-3 py-3 w-8">
                <input
                  type="checkbox"
                  aria-label="Select all on page"
                  data-testid="select-all-leads"
                  checked={
                    (leadsPage?.items?.length ?? 0) > 0
                    && leadsPage.items.every((l) => checked.has(l.id))
                  }
                  onChange={(e) => {
                    const ids = (leadsPage?.items ?? []).map((l) => l.id);
                    setChecked((prev) => {
                      const next = new Set(prev);
                      ids.forEach((id) => (e.target.checked ? next.add(id) : next.delete(id)));
                      return next;
                    });
                  }}
                />
              </th>
              <th className="px-4 py-3 text-left">Name</th>
              <th className="px-4 py-3 text-left">Email</th>
              <th className="px-4 py-3 text-left">Company</th>
              <th className="px-4 py-3 text-left">Campaign</th>
              <th className="px-4 py-3 text-left">Send</th>
              <th className="px-4 py-3 text-left">Notes</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              Array.from({ length: 6 }).map((_, i) => (
                <tr key={`sk-${i}`} className="border-b border-slate-100" data-testid="leads-skeleton-row">
                  <td className="px-3 py-3 w-8"><Skeleton className="h-4 w-4" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-40" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-28" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-28" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-16" /></td>
                  <td className="px-4 py-3"><Skeleton className="h-4 w-32" /></td>
                </tr>
              ))
            ) : error ? (
              <tr><td colSpan={7} className="p-0">
                <ErrorState message="Couldn't load leads." onRetry={() => refetch()} testId="leads-error" />
              </td></tr>
            ) : (leadsPage?.items?.length ?? 0) === 0 ? (
              <tr><td colSpan={7} className="p-0">
                <EmptyState
                  title="No leads found"
                  hint="Adjust your filters above, or add a lead with “+ New lead”."
                  icon="👤"
                  testId="leads-empty"
                />
              </td></tr>
            ) : (
              leadsPage.items.map((l) => (
                <tr
                  key={l.id}
                  onClick={() => setSelected(l)}
                  data-testid={`lead-row-${l.id}`}
                  className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer"
                >
                  <td className="px-3 py-3 w-8" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${l.email}`}
                      data-testid={`select-lead-${l.id}`}
                      checked={checked.has(l.id)}
                      onChange={() => toggleChecked(l.id)}
                    />
                  </td>
                  <td className="px-4 py-3 text-slate-700">
                    {l.first_name || l.last_name
                      ? `${l.first_name || ''} ${l.last_name || ''}`.trim()
                      : '—'}
                  </td>
                  <td className="px-4 py-3 text-slate-700">{l.email}</td>
                  <td className="px-4 py-3 text-slate-700">{l.company || '—'}</td>
                  <td className="px-4 py-3 text-slate-700">{l.campaign_name || '—'}</td>
                  <td className="px-4 py-3"><StatusPill value={l.send_status} /></td>
                  <td className="px-4 py-3 text-slate-600 max-w-[260px] truncate" title={l.notes || ''}>
                    {l.has_notes ? l.notes : <span className="text-slate-400">—</span>}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {leadsPage && leadsPage.total_pages > 1 && (
        <div className="flex justify-between items-center mt-4 text-sm text-slate-600">
          <div>
            Page {leadsPage.page} of {leadsPage.total_pages} · {leadsPage.total} total
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="px-3 py-1.5 border border-slate-300 rounded-lg disabled:opacity-50"
            >
              Previous
            </button>
            <button
              type="button"
              disabled={page >= (leadsPage?.total_pages ?? 1)}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1.5 border border-slate-300 rounded-lg disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {selected && (
        <LeadCrmModal lead={selected} onClose={() => setSelected(null)} />
      )}
      {creating && (
        <NewLeadModal onClose={() => setCreating(false)} />
      )}
    </div>
  );
}


function NewLeadModal({ onClose }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    email: '', first_name: '', last_name: '', company: '',
    job_title: '', phone: '', linkedin_url: '', notes: '',
  });
  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const createMut = useMutation({
    mutationFn: () => createCrmLead({
      email: form.email.trim(),
      first_name: form.first_name.trim() || null,
      last_name: form.last_name.trim() || null,
      company: form.company.trim() || null,
      job_title: form.job_title.trim() || null,
      phone: form.phone.trim() || null,
      linkedin_url: form.linkedin_url.trim() || null,
      notes: form.notes.trim() || null,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      toast.success('Lead created');
      onClose();
    },
    onError: (err) => {
      const d = err?.response?.data?.detail;
      toast.error(typeof d === 'string' ? d : 'Failed to create lead');
    },
  });

  const [emailTouched, setEmailTouched] = useState(false);
  const emailValid = form.email.trim().includes('@');
  const canSave = emailValid && !createMut.isPending;

  // Hand-rolled text fields → the shared Field + Input primitives (label +
  // wired htmlFor/aria; testids preserved by forwarding them through Input).
  const field = (key, label, props = {}) => (
    <Field label={label}>
      {(p) => (
        <Input
          {...p}
          data-testid={`new-lead-${key}`}
          value={form[key]}
          onChange={(e) => update(key, e.target.value)}
          {...props}
        />
      )}
    </Field>
  );

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-card shadow-overlay w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
        data-testid="new-lead-modal"
      >
        <div className="flex justify-between items-center mb-4">
          <h2 className="m-0 text-lg font-semibold text-slate-900">New lead</h2>
          <button type="button" onClick={onClose} aria-label="Close"
            className="text-slate-400 hover:text-slate-600 text-xl bg-transparent border-none cursor-pointer p-1">×</button>
        </div>
        <div className="space-y-3">
          <Field
            label="Email"
            required
            error={emailTouched && !emailValid ? 'Enter a valid email address.' : undefined}
          >
            {(p) => (
              <Input
                {...p}
                type="email"
                data-testid="new-lead-email"
                placeholder="jane@acme.com"
                value={form.email}
                onChange={(e) => update('email', e.target.value)}
                onBlur={() => setEmailTouched(true)}
              />
            )}
          </Field>
          <div className="grid grid-cols-2 gap-3">
            {field('first_name', 'First name')}
            {field('last_name', 'Last name')}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {field('company', 'Company')}
            {field('job_title', 'Job title')}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {field('phone', 'Phone')}
            {field('linkedin_url', 'LinkedIn URL')}
          </div>
          <Field label="Notes">
            {(p) => (
              <Textarea
                {...p}
                rows={3}
                data-testid="new-lead-notes"
                value={form.notes}
                onChange={(e) => update('notes', e.target.value)}
              />
            )}
          </Field>
          <p className="text-xs text-slate-500 m-0">
            Manually-created leads are CRM records — they don't enter any
            campaign's email pipeline unless you add them to one later.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button
              onClick={() => createMut.mutate()}
              disabled={!canSave}
              loading={createMut.isPending}
              data-testid="new-lead-save"
            >
              Create lead
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ── Lead detail modal helpers ────────────────────────────────────────────

const HISTORY_STATUS_CLASS = {
  success: 'text-emerald-700',
  warn: 'text-amber-700',
  fail: 'text-red-700',
};

const LINKEDIN_CONN_LABEL = {
  unknown: 'Unknown',
  invited: 'Invited',
  connected: 'Connected',
  declined: 'Declined',
};

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}


function LeadCrmModal({ lead, onClose }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  // Single source of truth: the cross-campaign detail endpoint with the
  // embedded timeline + suppression state.  (A legacy per-campaign
  // fallback used to live here, but it double-fetched on every open and
  // its response lacked is_suppressed/history — wrong button states
  // whenever it won the race.  Frontend and backend deploy atomically,
  // so the fallback bought nothing.)
  const { data: view } = useQuery({
    queryKey: ['lead-detail-v2', lead.id],
    queryFn: () => getLeadById(lead.id),
  });

  const [notes, setNotes] = useState('');
  const [hydrated, setHydrated] = useState(false);
  if (view && !hydrated) {
    setNotes(view.notes || '');
    setHydrated(true);
  }
  const dirty = hydrated && notes !== (view?.notes || '');

  // Editable contact / details.
  const CONTACT_FIELDS = ['first_name', 'last_name', 'email', 'company', 'job_title', 'phone', 'linkedin_url', 'company_website'];
  const [editingContact, setEditingContact] = useState(false);
  const [cform, setCform] = useState({});
  function startEditContact() {
    const src = view || lead;
    setCform(Object.fromEntries(CONTACT_FIELDS.map((k) => [k, src[k] || ''])));
    setEditingContact(true);
  }
  const editContactMut = useMutation({
    mutationFn: () => updateLeadFields(lead.id, Object.fromEntries(
      CONTACT_FIELDS.map((k) => [k, cform[k].trim()]),
    )),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      setEditingContact(false);
      toast.success('Contact details updated');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to update contact'),
  });

  const saveMutation = useMutation({
    // updateLeadEmail still takes campaign_id (per-campaign route).  Use
    // the lead's own campaign_id so the call lands correctly.
    mutationFn: () => updateLeadEmail(lead.campaign_id, lead.id, { notes }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      toast.success('Notes saved');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to save notes'),
  });

  const ignoreMutation = useMutation({
    mutationFn: () => ignoreLead(lead.id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      const halted = data?.leads_halted ?? 0;
      const camps = (data?.campaigns_affected || []).length;
      if (data?.already_suppressed) {
        toast.success(
          halted > 0
            ? `Already suppressed — halted ${halted} drifted lead row${halted === 1 ? '' : 's'}.`
            : 'This lead was already suppressed.',
        );
      } else {
        toast.success(
          `Lead ignored — suppressed and halted ${halted} sequence${halted === 1 ? '' : 's'}` +
          (camps > 1 ? ` across ${camps} campaigns.` : '.'),
        );
      }
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to ignore lead'),
  });

  const unignoreMutation = useMutation({
    mutationFn: () => unignoreLead(lead.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      toast.success('Lead un-suppressed — future campaigns can contact them again.');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to un-ignore lead'),
  });

  const crmStatusMut = useMutation({
    mutationFn: (status) => updateLeadCrmStatus(lead.id, status),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      toast.success('Lead status updated');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to update status'),
  });

  const convertMut = useMutation({
    mutationFn: () => convertLead(lead.id, {}),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', lead.id] });
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      queryClient.invalidateQueries({ queryKey: ['crm-opportunities'] });
      toast.success(`Converted — opportunity "${data.opportunity.name}" created. Find it on the Opportunities page.`);
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to convert lead'),
  });

  const trackMut = useMutation({
    // "Everything" watch on the tracked lead — the detectors use the
    // record's email/company, so no extra fields needed.
    mutationFn: () => createWatch({ watch_type: 'custom', lead_id: lead.id }),
    onSuccess: () => toast.success(
      'Tracking — job-change, funding, and hiring checks run daily. See the Signals page.',
    ),
    onError: (err) => {
      if (err?.response?.status === 409) {
        toast.success('Already tracking this lead — see the Signals page.');
      } else {
        toast.error(err?.response?.data?.detail || 'Failed to start tracking');
      }
    },
  });

  const isSuppressed = view?.is_suppressed === true;
  const suppressionReason = view?.suppression_reason;
  const crmStatus = view?.crm_status || 'new';
  const isConverted = crmStatus === 'converted';

  // Derived display values (safe on partial detail loads).
  const fullName = (lead.first_name || lead.last_name)
    ? `${lead.first_name || ''} ${lead.last_name || ''}`.trim()
    : lead.email;
  const history = view?.history || [];
  const counts = view?.history_counts || {};
  const sigSummary = view?.research_summary || {};

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl p-6 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
        data-testid="lead-crm-modal"
      >
        {/* Header */}
        <div className="flex justify-between items-start mb-4">
          <div>
            <h2 className="m-0 text-lg font-semibold text-slate-900">{fullName}</h2>
            <div className="text-sm text-slate-500 mt-0.5">
              {lead.email}
              {(view?.job_title || lead.job_title) && (
                <> · {view?.job_title || lead.job_title}</>
              )}
              {(view?.company || lead.company) && (
                <> at {view?.company || lead.company}</>
              )}
              {lead.campaign_name && (
                <> · <span className="text-slate-400">campaign</span> {lead.campaign_name}</>
              )}
            </div>
            {/* Stat pills: quick at-a-glance roll-up */}
            <div className="mt-2 flex flex-wrap items-center gap-1.5" data-testid="lead-stat-pills">
              <StatusPill value={lead.send_status} />
              {isConverted ? (
                <span
                  data-testid="crm-status-converted-badge"
                  className={`px-2 py-0.5 rounded text-xs font-semibold ${CRM_STATUS_CLASSES.converted}`}
                  title="This lead has been converted to an opportunity — see the Opportunities page."
                >
                  ✦ Converted
                </span>
              ) : (
                <select
                  value={crmStatus}
                  onChange={(e) => crmStatusMut.mutate(e.target.value)}
                  disabled={!view || crmStatusMut.isPending}
                  data-testid="crm-status-select"
                  className={`px-1.5 py-0.5 rounded text-xs font-medium border-0 cursor-pointer ${CRM_STATUS_CLASSES[crmStatus] || 'bg-slate-100 text-slate-700'}`}
                  title="CRM lead status"
                >
                  {SETTABLE_CRM_STATUSES.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              )}
              {isSuppressed && (
                <span
                  data-testid="lead-suppressed-badge"
                  className="px-2 py-0.5 rounded text-xs font-semibold bg-red-100 text-red-800"
                  title={
                    suppressionReason
                      ? `Suppressed (${suppressionReason}) — workspace-wide; future campaigns cannot contact this email.`
                      : 'Suppressed workspace-wide; future campaigns cannot contact this email.'
                  }
                >
                  🚫 Suppressed
                </span>
              )}
              {counts.opened > 0 && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-emerald-50 text-emerald-700">
                  👀 {counts.opened} open{counts.opened === 1 ? '' : 's'}
                </span>
              )}
              {counts.clicked > 0 && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-emerald-50 text-emerald-700">
                  🖱️ {counts.clicked} click{counts.clicked === 1 ? '' : 's'}
                </span>
              )}
              {counts.replied > 0 && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-brand-50 text-brand-700">
                  💬 {counts.replied} repl{counts.replied === 1 ? 'y' : 'ies'}
                </span>
              )}
              {(counts.hard_bounce || counts.soft_bounce) && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-50 text-red-700">
                  ⚠️ bounced
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => trackMut.mutate()}
              disabled={trackMut.isPending}
              data-testid="lead-track-signals-btn"
              title="Watch this lead for job changes, funding, and hiring signals (daily check)"
              className="px-3 py-1.5 text-xs font-medium border border-violet-300 text-violet-700 hover:bg-violet-50 rounded-lg disabled:opacity-50"
            >
              {trackMut.isPending ? 'Tracking…' : '⚡ Track signals'}
            </button>
            {!isConverted && (
              <button
                type="button"
                onClick={() => {
                  if (confirm(
                    `Convert ${lead.email} to an opportunity?\n\n` +
                    'Contact info is copied onto a new deal (stage: Qualification). ' +
                    'The lead is marked Converted and the deal appears on the ' +
                    'Opportunities page where you can set amount + close date.',
                  )) convertMut.mutate();
                }}
                disabled={!view || convertMut.isPending}
                data-testid="lead-convert-btn"
                className="px-3 py-1.5 text-xs font-medium border border-emerald-300 text-emerald-700 hover:bg-emerald-50 rounded-lg disabled:opacity-50"
              >
                {convertMut.isPending ? 'Converting…' : '✦ Convert to opportunity'}
              </button>
            )}
            {/* Ignore / Un-ignore — primary destructive action up top so
                the user finds it without scrolling.  Disabled while the
                detail is still loading so the user can't kick off the
                action before seeing the current suppression state. */}
            {isSuppressed ? (
              <button
                type="button"
                onClick={() => {
                  if (confirm(
                    'Remove this lead from the suppression list?  ' +
                    'Future campaigns will be able to contact this email again.  ' +
                    'This does NOT reactivate any already-halted sequence steps — ' +
                    "you'd need to re-enroll the lead via the Activity tab.",
                  )) unignoreMutation.mutate();
                }}
                disabled={!view || unignoreMutation.isPending}
                data-testid="lead-unignore-btn"
                className="px-3 py-1.5 text-xs font-medium border border-slate-300 text-slate-700 hover:bg-slate-50 rounded-lg disabled:opacity-50"
              >
                {unignoreMutation.isPending ? 'Un-ignoring…' : 'Un-ignore'}
              </button>
            ) : (
              <button
                type="button"
                onClick={() => {
                  if (confirm(
                    `Ignore ${lead.email}?\n\n` +
                    "• They'll be added to the workspace suppression list.\n" +
                    '• Any active sequence steps for this email (across all campaigns) will be halted.\n' +
                    "• Future campaigns that include this email will skip them automatically.\n\n" +
                    'You can un-ignore later from this same modal.',
                  )) ignoreMutation.mutate();
                }}
                disabled={!view || ignoreMutation.isPending}
                data-testid="lead-ignore-btn"
                className="px-3 py-1.5 text-xs font-medium border border-red-300 text-red-700 hover:bg-red-50 rounded-lg disabled:opacity-50"
              >
                {ignoreMutation.isPending ? 'Ignoring…' : '🚫 Ignore lead'}
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="text-slate-400 hover:text-slate-600 text-xl bg-transparent border-none cursor-pointer p-1"
              aria-label="Close"
            >×</button>
          </div>
        </div>

        {/* Contact / outreach info card */}
        <div
          data-testid="lead-contact-section"
          className="bg-slate-50 border border-slate-200 rounded-lg p-3 mb-4 text-xs"
        >
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide">
              Contact &amp; details
            </span>
            {!editingContact && (
              <button
                type="button"
                data-testid="lead-edit-contact-btn"
                onClick={startEditContact}
                className="text-xs text-brand-600 hover:underline bg-transparent border-none cursor-pointer p-0"
              >
                Edit
              </button>
            )}
          </div>

          {editingContact ? (
            <div data-testid="lead-contact-edit" className="space-y-2">
              <div className="grid grid-cols-2 gap-2">
                {CONTACT_FIELDS.map((k) => (
                  <label key={k} className="block">
                    <span className="text-slate-500 capitalize">{k.replace(/_/g, ' ')}</span>
                    <input
                      type="text"
                      data-testid={`lead-edit-${k}`}
                      value={cform[k] ?? ''}
                      onChange={(e) => setCform((f) => ({ ...f, [k]: e.target.value }))}
                      className="mt-0.5 block w-full border border-slate-300 rounded px-2 py-1 text-xs"
                    />
                  </label>
                ))}
              </div>
              <div className="flex justify-end gap-2 pt-1">
                <button
                  type="button"
                  data-testid="lead-edit-cancel-btn"
                  onClick={() => setEditingContact(false)}
                  className="px-2.5 py-1 text-xs border border-slate-300 rounded text-slate-600 hover:bg-white"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  data-testid="lead-edit-save-btn"
                  onClick={() => editContactMut.mutate()}
                  disabled={editContactMut.isPending || !(cform.email || '').trim()}
                  className="px-2.5 py-1 text-xs bg-brand-600 text-white rounded hover:bg-brand-700 disabled:opacity-50"
                >
                  {editContactMut.isPending ? 'Saving…' : 'Save'}
                </button>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              <InfoRow label="Email">
                {(view?.email || lead.email)
                  ? <a href={`mailto:${view?.email || lead.email}`} className="text-brand-600 hover:underline">{view?.email || lead.email}</a>
                  : <span className="text-slate-400">—</span>}
              </InfoRow>
              {(view?.company || lead.company) && <InfoRow label="Company">{view?.company || lead.company}</InfoRow>}
              {(view?.job_title || lead.job_title) && <InfoRow label="Title">{view?.job_title || lead.job_title}</InfoRow>}
              {view?.phone && (
                <InfoRow label="Phone">
                  <a href={`tel:${view.phone}`} className="text-brand-600 hover:underline">{view.phone}</a>
                </InfoRow>
              )}
              {view?.linkedin_url && (
                <InfoRow label="LinkedIn">
                  <a
                    href={view.linkedin_url}
                    target="_blank" rel="noopener noreferrer"
                    data-testid="lead-linkedin-link"
                    className="text-brand-600 hover:underline"
                  >
                    Open profile ↗
                  </a>
                  {view?.linkedin_connection_status && view.linkedin_connection_status !== 'unknown' && (
                    <span className="ml-2 text-slate-500">
                      ({LINKEDIN_CONN_LABEL[view.linkedin_connection_status] || view.linkedin_connection_status})
                    </span>
                  )}
                </InfoRow>
              )}
              {view?.company_website && (
                <InfoRow label="Website">
                  <a
                    href={
                      view.company_website.startsWith('http')
                        ? view.company_website
                        : `https://${view.company_website}`
                    }
                    target="_blank" rel="noopener noreferrer"
                    className="text-brand-600 hover:underline"
                  >
                    {view.company_website} ↗
                  </a>
                </InfoRow>
              )}
              {sigSummary.industry && <InfoRow label="Industry">{sigSummary.industry}</InfoRow>}
              {sigSummary.size_hint && <InfoRow label="Size">{sigSummary.size_hint}</InfoRow>}
            </div>
          )}
        </div>

        {/* Composed email preview */}
        {view?.composed_subject || view?.composed_body ? (
          <details className="border border-slate-200 rounded-lg overflow-hidden mb-4" open>
            <summary className="bg-slate-50 border-b border-slate-200 px-4 py-2.5 cursor-pointer text-xs font-semibold text-slate-500 uppercase tracking-wide">
              Composed email
            </summary>
            <div className="px-4 py-2.5 border-b border-slate-100">
              <span className="text-xs font-semibold text-slate-500 mr-2">Subject:</span>
              <span className="text-sm text-slate-900 font-medium">{view.composed_subject || '—'}</span>
            </div>
            <div className="p-4">
              <pre className="text-sm text-slate-800 whitespace-pre-wrap font-sans leading-relaxed m-0">
                {view.composed_body || '—'}
              </pre>
            </div>
          </details>
        ) : (
          <div className="text-sm text-slate-500 italic py-2 mb-4">No email composed yet.</div>
        )}

        {/* Activity timeline */}
        <details className="border border-slate-200 rounded-lg overflow-hidden mb-4" open>
          <summary className="bg-slate-50 border-b border-slate-200 px-4 py-2.5 cursor-pointer text-xs font-semibold text-slate-500 uppercase tracking-wide flex items-center justify-between">
            <span>Activity history</span>
            <span className="text-slate-400 normal-case font-normal">
              {history.length} event{history.length === 1 ? '' : 's'}
            </span>
          </summary>
          <div data-testid="lead-history-list" className="divide-y divide-slate-100">
            {history.length === 0 ? (
              <div className="p-4 text-sm text-slate-500 italic">
                No activity yet — this lead hasn't moved through any sequence step or
                received any opens/clicks.
              </div>
            ) : (
              history.map((h, i) => (
                <div key={i} className="flex items-start gap-3 px-4 py-2.5 text-sm">
                  <span className="text-lg leading-tight">{h.icon}</span>
                  <div className="flex-1 min-w-0">
                    <div className={`font-medium ${HISTORY_STATUS_CLASS[h.status] || 'text-slate-900'}`}>
                      {h.action}
                    </div>
                    {h.detail && (
                      <div className="text-xs text-slate-500 mt-0.5 break-words" title={h.detail}>
                        {h.detail.length > 200 ? `${h.detail.slice(0, 200)}…` : h.detail}
                      </div>
                    )}
                  </div>
                  <div className="text-xs text-slate-400 whitespace-nowrap">{fmtTime(h.at)}</div>
                </div>
              ))
            )}
          </div>
        </details>

        {/* Research details */}
        {(sigSummary.person_news?.length > 0
          || sigSummary.company_news?.length > 0
          || sigSummary.company_description) && (
          <details className="border border-slate-200 rounded-lg overflow-hidden mb-4">
            <summary className="bg-slate-50 border-b border-slate-200 px-4 py-2.5 cursor-pointer text-xs font-semibold text-slate-500 uppercase tracking-wide">
              Research findings
              {sigSummary.from_cache && (
                <span className="ml-2 text-amber-600 normal-case font-normal">(from cache)</span>
              )}
            </summary>
            <div className="p-4 space-y-3 text-sm">
              {sigSummary.company_description && (
                <div>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">
                    Company
                  </div>
                  <p className="text-slate-700 m-0">{sigSummary.company_description}</p>
                </div>
              )}
              {sigSummary.person_news?.length > 0 && (
                <div>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">
                    Person news
                  </div>
                  <ul className="list-disc list-inside text-slate-700 m-0 space-y-0.5">
                    {sigSummary.person_news.map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                </div>
              )}
              {sigSummary.company_news?.length > 0 && (
                <div>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">
                    Company news
                  </div>
                  <ul className="list-disc list-inside text-slate-700 m-0 space-y-0.5">
                    {sigSummary.company_news.map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </details>
        )}

        {/* Manual CRM activity log (calls / emails / meetings / notes / tasks) */}
        <div className="mb-4">
          <ActivityLog leadId={lead.id} />
        </div>

        {/* Notes */}
        <div className="space-y-2">
          <label className="block text-xs font-semibold text-slate-500 uppercase tracking-wide">
            Notes
          </label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={5}
            placeholder="Met at Lattice summit; warm intro from Sara…"
            data-testid="lead-notes-textarea"
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 font-[inherit]"
          />
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => saveMutation.mutate()}
              disabled={!dirty || saveMutation.isPending}
              data-testid="save-notes-btn"
              className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50"
            >
              {saveMutation.isPending ? 'Saving…' : 'Save notes'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


/** Small key/value row used in the contact-info card.  Renders nothing
 *  when ``children`` is empty so call-sites can guard with ``&&`` and
 *  still get clean layout. */
function InfoRow({ label, children }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-slate-500 w-20 shrink-0">{label}</span>
      <span className="text-slate-900 truncate">{children}</span>
    </div>
  );
}
