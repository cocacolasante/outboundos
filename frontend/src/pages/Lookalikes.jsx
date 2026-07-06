import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  acceptCandidate,
  getIcpProfile,
  listCandidates,
  regenerateIcpProfile,
  rejectCandidate,
} from '../api/icp.js';
import { useToast } from '../components/Toast.jsx';

const CRITERIA_LABELS = {
  industries: 'Industries',
  employee_count_band: 'Employee band',
  title_patterns: 'Titles',
  geographies: 'Geographies',
  funding_stages: 'Funding stages',
  keywords: 'Keywords',
};

function scoreColor(score) {
  if (score >= 70) return 'bg-emerald-100 text-emerald-700';
  if (score >= 40) return 'bg-amber-100 text-amber-700';
  return 'bg-slate-100 text-slate-500';
}

function IcpCard() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: profile } = useQuery({
    queryKey: ['icp-profile'],
    queryFn: getIcpProfile,
  });

  const regenMut = useMutation({
    mutationFn: regenerateIcpProfile,
    onSuccess: (p) => {
      queryClient.setQueryData(['icp-profile'], p);
      toast.success(
        p.status === 'ready'
          ? 'ICP regenerated from closed-won deals'
          : `Not enough closed-won deals yet (need ${p.min_won_deals})`,
      );
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });

  if (!profile) return null;

  return (
    <div data-testid="icp-card" className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-6">
      <div className="flex items-start justify-between mb-2">
        <div>
          <h2 className="text-base font-semibold text-slate-900 m-0">Ideal customer profile</h2>
          <p className="text-xs text-slate-400 m-0 mt-0.5">
            {profile.status === 'ready'
              ? `Derived from ${profile.won_deal_count} closed-won deal${profile.won_deal_count === 1 ? '' : 's'}`
              : profile.status === 'insufficient_data'
                ? `Needs ≥ ${profile.min_won_deals} closed-won deals (have ${profile.won_deal_count ?? 0})`
                : 'Not built yet — regenerate once you have closed-won deals'}
          </p>
        </div>
        <button
          type="button"
          data-testid="regenerate-icp-btn"
          onClick={() => regenMut.mutate()}
          disabled={regenMut.isPending}
          className="text-sm px-3 py-1.5 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50 disabled:opacity-50"
        >
          {regenMut.isPending ? 'Working…' : 'Regenerate'}
        </button>
      </div>
      {profile.status === 'ready' && profile.criteria && (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 m-0" data-testid="icp-criteria">
          {Object.entries(CRITERIA_LABELS).map(([key, label]) => {
            const val = profile.criteria[key];
            const text = Array.isArray(val) ? val.join(', ') : val;
            if (!text) return null;
            return (
              <div key={key} className="flex gap-2 text-sm">
                <dt className="text-slate-500 w-28 shrink-0">{label}</dt>
                <dd className="m-0 text-slate-800">{text}</dd>
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}

function CandidateRow({ candidate }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['lookalike-candidates'] });

  const acceptMut = useMutation({
    mutationFn: () => acceptCandidate(candidate.id),
    onSuccess: () => {
      invalidate();
      toast.success('Accepted — CRM lead created (add to a campaign when ready)');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Accept failed'),
  });
  const rejectMut = useMutation({
    mutationFn: () => rejectCandidate(candidate.id),
    onSuccess: () => { invalidate(); toast.success('Rejected'); },
  });

  return (
    <tr data-testid={`candidate-row-${candidate.id}`} className="border-b border-slate-100">
      <td className="py-2.5 pr-3">
        <span
          data-testid={`fit-score-${candidate.id}`}
          className={`inline-block px-2 py-0.5 rounded-full text-xs font-bold ${scoreColor(candidate.fit_score)}`}
        >
          {candidate.fit_score}
        </span>
      </td>
      <td className="py-2.5 pr-3">
        <div className="font-medium text-slate-800">{candidate.company}</div>
        {candidate.company_website && (
          <a href={candidate.company_website} target="_blank" rel="noreferrer" className="text-xs text-brand-600">
            {candidate.company_website.replace(/^https?:\/\//, '')}
          </a>
        )}
      </td>
      <td className="py-2.5 pr-3 text-sm text-slate-600">
        {candidate.contact_name || '—'}
        {candidate.job_title && <span className="text-slate-400"> · {candidate.job_title}</span>}
      </td>
      <td className="py-2.5 pr-3 text-xs text-slate-500 max-w-56">{candidate.fit_reason}</td>
      <td className="py-2.5 text-right">
        {candidate.status === 'new' ? (
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              data-testid={`accept-${candidate.id}`}
              onClick={() => acceptMut.mutate()}
              disabled={acceptMut.isPending}
              className="text-sm px-3 py-1 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              Accept
            </button>
            <button
              type="button"
              data-testid={`reject-${candidate.id}`}
              onClick={() => rejectMut.mutate()}
              className="text-sm px-3 py-1 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50"
            >
              Reject
            </button>
          </div>
        ) : (
          <span className="text-xs text-slate-400 italic">{candidate.status}</span>
        )}
      </td>
    </tr>
  );
}

export default function Lookalikes() {
  const [statusFilter, setStatusFilter] = useState('new');
  const { data } = useQuery({
    queryKey: ['lookalike-candidates', statusFilter],
    queryFn: () => listCandidates(statusFilter ? { status: statusFilter } : {}),
  });
  const items = data?.items || [];

  return (
    <div data-testid="lookalikes-page" className="p-8 max-w-5xl">
      <h1 className="text-2xl font-bold text-slate-900 mb-1">Lookalikes</h1>
      <p className="text-slate-500 text-sm mt-0 mb-6">
        Companies and contacts that look like your closed-won customers.
        Accepting stages a CRM lead — adding to a campaign stays manual.
      </p>

      <IcpCard />

      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold text-slate-900 m-0">Candidates</h2>
        <select
          data-testid="candidate-status-filter"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="border border-slate-300 rounded-lg px-3 py-1.5 text-sm bg-white"
        >
          <option value="new">New</option>
          <option value="accepted">Accepted</option>
          <option value="rejected">Rejected</option>
          <option value="">All</option>
        </select>
      </div>

      {items.length === 0 ? (
        <p data-testid="candidates-empty" className="text-sm text-slate-400 text-center py-10 bg-white border border-dashed border-slate-300 rounded-xl">
          No candidates here. Discovery runs daily once the ICP is ready
          (≥3 closed-won deals).
        </p>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="text-xs text-slate-500 uppercase tracking-wide border-b border-slate-200">
                <th className="py-2.5 px-3 pl-4">Fit</th>
                <th className="py-2.5 pr-3">Company</th>
                <th className="py-2.5 pr-3">Contact</th>
                <th className="py-2.5 pr-3">Why</th>
                <th className="py-2.5 pr-4" />
              </tr>
            </thead>
            <tbody className="[&>tr>td:first-child]:pl-4 [&>tr>td:last-child]:pr-4">
              {items.map((c) => <CandidateRow key={c.id} candidate={c} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
