import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteCampaignLead, listCampaignLeads } from '../api/campaigns.js';

const SEND_STATUSES = ['', 'pending', 'scheduled', 'sent', 'failed'];

const PILL = {
  pending:   'bg-slate-100 text-slate-500',
  running:   'bg-brand-100 text-brand-700',
  done:      'bg-emerald-100 text-emerald-700',
  sent:      'bg-emerald-100 text-emerald-700',
  failed:    'bg-red-100 text-red-600',
  skipped:   'bg-slate-100 text-slate-500',
  scheduled: 'bg-amber-100 text-amber-700',
};

function StatusPill({ value }) {
  const cls = PILL[value] || 'bg-slate-100 text-slate-500';
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}`}>
      {value}
    </span>
  );
}

const STAGE_PILL = {
  active:    'bg-brand-100 text-brand-700',
  completed: 'bg-emerald-100 text-emerald-700',
  halted:    'bg-red-100 text-red-600',
  pending:   'bg-slate-100 text-slate-500',
};

function StagePill({ status, label }) {
  if (!label) return <span className="text-slate-400">—</span>;
  const cls = STAGE_PILL[status] || 'bg-slate-100 text-slate-600';
  return (
    <span
      data-testid="row-sequence-stage"
      title={status ? `Sequence: ${status}` : undefined}
      className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}`}
    >
      {label}
    </span>
  );
}

function toCsv(rows) {
  const headers = ['email', 'first_name', 'last_name', 'company', 'job_title', 'research_status', 'compose_status', 'send_status', 'created_at'];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const cells = headers.map((h) => {
      const v = r[h] == null ? '' : String(r[h]);
      return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
    });
    lines.push(cells.join(','));
  }
  return lines.join('\n');
}

export default function LeadTable({ campaignId, replyTrackingEnabled, onViewLead }) {
  const [page, setPage] = useState(1);
  const [pageSize] = useState(50);
  const [search, setSearch] = useState('');
  const [sendStatus, setSendStatus] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(null); // lead object or null
  const queryClient = useQueryClient();

  const queryParams = {
    page,
    page_size: pageSize,
    ...(search ? { search } : {}),
    ...(sendStatus ? { send_status: sendStatus } : {}),
  };
  const { data, isLoading } = useQuery({
    queryKey: ['campaign-leads', campaignId, queryParams],
    queryFn: () => listCampaignLeads(campaignId, queryParams),
    keepPreviousData: true,
  });

  const deleteMutation = useMutation({
    mutationFn: (leadId) => deleteCampaignLead(campaignId, leadId),
    onSuccess: () => {
      // Refresh both this table AND the parent campaign progress widget.
      queryClient.invalidateQueries({ queryKey: ['campaign-leads', campaignId] });
      queryClient.invalidateQueries({ queryKey: ['campaign', campaignId] });
      setConfirmDelete(null);
    },
  });

  function exportCsv() {
    if (!data?.items) return;
    const csv = toCsv(data.items);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `campaign-${campaignId}-leads.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 0;

  return (
    <div data-testid="lead-table">
      <div className="flex gap-3 mb-4 items-center">
        <input
          type="search"
          placeholder="Search by email or name"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          aria-label="Search leads"
          className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
        />
        <select
          aria-label="Filter by send status"
          value={sendStatus}
          onChange={(e) => { setSendStatus(e.target.value); setPage(1); }}
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
        >
          {SEND_STATUSES.map((s) => (
            <option key={s} value={s}>{s ? s : 'All statuses'}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={exportCsv}
          disabled={!data?.items?.length}
          className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Export CSV
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-200">
        <table className="w-full text-sm">
          <thead>
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Name</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Email</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Company</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Research</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Compose</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Send</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Stage</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200"></th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td colSpan={8} className="px-4 py-3 text-slate-700 border-b border-slate-100 text-center">Loading…</td></tr>
            ) : data?.items?.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-3 text-slate-500 border-b border-slate-100 text-center">No leads.</td></tr>
            ) : (
              data?.items?.map((lead) => (
                <tr key={lead.id} className="hover:bg-slate-50">
                  <td className="px-4 py-3 text-slate-700 border-b border-slate-100">
                    {lead.first_name || lead.last_name
                      ? `${lead.first_name || ''} ${lead.last_name || ''}`.trim()
                      : '—'}
                  </td>
                  <td className="px-4 py-3 text-slate-700 border-b border-slate-100">{lead.email}</td>
                  <td className="px-4 py-3 text-slate-700 border-b border-slate-100">{lead.company || '—'}</td>
                  <td className="px-4 py-3 border-b border-slate-100">
                    <StatusPill value={lead.research_status} />
                  </td>
                  <td className="px-4 py-3 border-b border-slate-100">
                    <StatusPill value={lead.compose_status} />
                  </td>
                  <td className="px-4 py-3 border-b border-slate-100">
                    <span data-testid="row-send-status">
                      <StatusPill value={lead.send_status} />
                    </span>
                  </td>
                  <td className="px-4 py-3 border-b border-slate-100">
                    <StagePill status={lead.sequence_status} label={lead.sequence_stage} />
                  </td>
                  <td className="px-4 py-3 border-b border-slate-100 text-right">
                    <div className="flex items-center justify-end gap-3">
                      {lead.compose_status === 'done' && onViewLead && (
                        <button
                          type="button"
                          onClick={() => onViewLead(lead)}
                          className="text-xs text-brand-600 hover:text-brand-800 hover:underline bg-transparent border-none cursor-pointer p-0"
                        >
                          View email
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setConfirmDelete(lead)}
                        data-testid={`delete-lead-${lead.id}`}
                        aria-label={`Delete ${lead.email}`}
                        title="Remove from campaign and halt future steps"
                        className="text-xs text-red-600 hover:text-red-800 hover:underline bg-transparent border-none cursor-pointer p-0"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {confirmDelete && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          onClick={() => !deleteMutation.isPending && setConfirmDelete(null)}
        >
          <div
            className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6"
            onClick={(e) => e.stopPropagation()}
            data-testid="delete-lead-modal"
          >
            <h2 className="m-0 text-base font-semibold text-slate-900">
              Delete this lead?
            </h2>
            <p className="text-sm text-slate-600 mt-2">
              This removes <strong>{confirmDelete.email}</strong> from the
              campaign and halts every future step in the sequence (DM,
              follow-up emails, profile views).  Already-sent emails, opens,
              clicks, and reply events for this lead are also wiped.
            </p>
            <p className="text-sm text-slate-600 mt-2">
              This can't be undone — re-importing from CSV would create a
              new lead with no history.
            </p>
            {deleteMutation.error && (
              <p className="text-sm text-red-600 mt-3 bg-red-50 border border-red-200 rounded-md p-2">
                {deleteMutation.error?.response?.data?.detail || deleteMutation.error.message}
              </p>
            )}
            <div className="flex justify-end gap-2 mt-5">
              <button
                type="button"
                onClick={() => setConfirmDelete(null)}
                disabled={deleteMutation.isPending}
                className="px-4 py-2 text-sm bg-white hover:bg-slate-50 text-slate-700 border border-slate-300 rounded-md disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => deleteMutation.mutate(confirmDelete.id)}
                disabled={deleteMutation.isPending}
                data-testid="confirm-delete-lead"
                className="px-4 py-2 text-sm bg-red-600 hover:bg-red-700 text-white font-semibold rounded-md disabled:opacity-50"
              >
                {deleteMutation.isPending ? 'Deleting…' : 'Delete lead'}
              </button>
            </div>
          </div>
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex justify-between items-center mt-4">
          <span className="text-sm text-slate-500">
            Page {page} of {totalPages} · {total} leads total
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label="Previous page"
            >
              ← Prev
            </button>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label="Next page"
            >
              Next →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
