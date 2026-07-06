import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  getAnalytics,
  listCampaignErrors,
  retryFailedLeads,
} from '../api/campaigns.js';
import { getSequenceAnalytics } from '../api/sequences.js';
import MetricsGrid from '../components/MetricsGrid.jsx';
import LeadTable from '../components/LeadTable.jsx';
import { useToast } from '../components/Toast.jsx';
import { formatPercent as pct } from '../utils/format.js';

function reputationColorStyle(score) {
  if (score == null) return '#94a3b8';
  if (score >= 80) return '#22c55e';
  if (score >= 50) return '#f59e0b';
  return '#dc2626';
}

function ReputationCard({ score, rates }) {
  return (
    <div data-testid="reputation-card" className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4">
      <h3 className="text-base font-semibold text-slate-900 mb-4">Sender reputation</h3>
      <div
        data-testid="reputation-score"
        className="text-6xl font-bold mb-4"
        style={{ color: reputationColorStyle(score) }}
      >
        {score == null ? '—' : score}
      </div>
      <div className="flex gap-6 text-sm">
        <div>
          <div className="font-semibold text-slate-900">{pct(rates.delivery_rate)}</div>
          <div className="text-slate-500 text-xs uppercase tracking-wide mt-0.5">delivered</div>
        </div>
        <div>
          <div className="font-semibold text-slate-900">{pct(rates.spam_rate)}</div>
          <div className="text-slate-500 text-xs uppercase tracking-wide mt-0.5">spam</div>
        </div>
        <div>
          <div className="font-semibold text-slate-900">{pct(rates.bounce_rate)}</div>
          <div className="text-slate-500 text-xs uppercase tracking-wide mt-0.5">bounced</div>
        </div>
      </div>
    </div>
  );
}

function QualityBreakdown({ items }) {
  if (!items || items.length === 0) return null;
  const labels = { rich: 'Rich', partial: 'Partial', low: 'Generic' };
  const bgClass = { rich: 'bg-emerald-50 border-emerald-200', partial: 'bg-yellow-50 border-yellow-200', low: 'bg-slate-50 border-slate-200' };
  return (
    <div data-testid="quality-breakdown" className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4">
      <h3 className="text-base font-semibold text-slate-900 mb-4">Research quality vs. open rate</h3>
      <div className="flex gap-4">
        {items.map((item) => (
          <div key={item.quality} className={`flex-1 p-4 rounded-lg border ${bgClass[item.quality] || 'bg-slate-50 border-slate-200'}`}>
            <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">{labels[item.quality] || item.quality}</div>
            <div className="text-2xl font-bold text-slate-900 mb-1">{item.count}</div>
            <div className="text-xs text-slate-500">Open: {pct(item.open_rate)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function BestSubjects({ subjects }) {
  if (!subjects || subjects.length === 0) return null;
  return (
    <div data-testid="best-subjects" className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4">
      <h3 className="text-base font-semibold text-slate-900 mb-4">Best subject lines</h3>
      <div className="overflow-hidden rounded-lg border border-slate-200">
        <table className="w-full text-sm">
          <thead>
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Subject</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200 w-20">Sent</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200 w-24">Open rate</th>
            </tr>
          </thead>
          <tbody>
            {subjects.map((s) => (
              <tr key={s.subject} className="hover:bg-slate-50">
                <td className="px-4 py-3 text-slate-700 border-b border-slate-100">{s.subject}</td>
                <td className="px-4 py-3 text-slate-700 border-b border-slate-100 tabular">{s.sent}</td>
                <td className="px-4 py-3 text-slate-700 border-b border-slate-100 tabular">{pct(s.open_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Timeline({ points, replyTrackingEnabled }) {
  if (!points || points.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4">
        <h3 className="text-base font-semibold text-slate-900 mb-4">Timeline</h3>
        <p className="text-sm text-slate-400">No events yet.</p>
      </div>
    );
  }
  return (
    <div data-testid="timeline-chart" className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4">
      <h3 className="text-base font-semibold text-slate-900 mb-4">Last 30 days</h3>
      <div className="mt-3">
        <LineChart width={680} height={220} data={points}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="date" />
          <YAxis />
          <Tooltip />
          <Legend />
          <Line type="monotone" dataKey="opens" stroke="#14b8a6" strokeWidth={2} />
          <Line type="monotone" dataKey="clicks" stroke="#8b5cf6" strokeWidth={2} />
          {replyTrackingEnabled && (
            <Line type="monotone" dataKey="replies" stroke="#f59e0b" strokeWidth={2} />
          )}
        </LineChart>
      </div>
    </div>
  );
}

function FailedLeadsBanner({ id }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: errors = [] } = useQuery({
    queryKey: ['campaign-errors', id],
    queryFn: () => listCampaignErrors(id),
    refetchInterval: 30000,
  });

  const retryMutation = useMutation({
    mutationFn: () => retryFailedLeads(id),
    onSuccess: (data) => {
      const total =
        (data?.research_retried || 0) +
        (data?.compose_retried || 0) +
        (data?.send_retried || 0);
      toast.success(`Re-queued ${total} failed lead${total === 1 ? '' : 's'}`);
      queryClient.invalidateQueries({ queryKey: ['campaign-errors', id] });
      queryClient.invalidateQueries({ queryKey: ['analytics', id] });
    },
    onError: (e) => toast.error(e?.response?.data?.detail || e.message || 'Retry failed'),
  });

  if (errors.length === 0) return null;

  return (
    <div data-testid="failed-leads-banner" className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800 mb-4 justify-between">
      <div>
        <strong className="font-semibold">{errors.length}</strong> lead{errors.length === 1 ? '' : 's'} failed in this campaign.
      </div>
      <button
        type="button"
        onClick={() => retryMutation.mutate()}
        disabled={retryMutation.isPending}
        className="inline-flex items-center px-4 py-2 bg-red-600 hover:bg-red-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50"
        data-testid="retry-failed-button"
      >
        {retryMutation.isPending ? 'Retrying…' : 'Retry failed'}
      </button>
    </div>
  );
}


const SEQ_KIND_LABELS = {
  email: 'Email',
  email_reply: 'Reply (in-thread)',
  wait: 'Wait',
  linkedin_view_profile: 'LinkedIn: view',
  linkedin_follow_profile: 'LinkedIn: follow',
  linkedin_react_post: 'LinkedIn: react',
  linkedin_comment_post: 'LinkedIn: comment',
  linkedin_connect: 'LinkedIn: connect',
  linkedin_dm: 'LinkedIn: DM',
  linkedin_inmail: 'LinkedIn: InMail',
  linkedin_invite_to_page: 'LinkedIn: page invite',
};

const SEQ_KIND_COLORS = {
  email: 'bg-brand-100 text-brand-700',
  email_reply: 'bg-indigo-100 text-indigo-700',
  wait: 'bg-slate-100 text-slate-500',
  linkedin_view_profile: 'bg-sky-100 text-sky-700',
  linkedin_follow_profile: 'bg-sky-100 text-sky-700',
  linkedin_react_post: 'bg-sky-100 text-sky-700',
  linkedin_comment_post: 'bg-indigo-100 text-indigo-700',
  linkedin_connect: 'bg-indigo-100 text-indigo-700',
  linkedin_dm: 'bg-purple-100 text-purple-700',
  linkedin_inmail: 'bg-purple-100 text-purple-700',
  linkedin_invite_to_page: 'bg-indigo-100 text-indigo-700',
};

function stepName(node, index) {
  const base = SEQ_KIND_LABELS[node.kind] ?? node.kind;
  // A builder title (when set) is the most descriptive; fall back to
  // "<n>. <kind>" so multiple same-kind steps stay distinguishable.
  if (node.title) return node.title;
  return `${index + 1}. ${base}`;
}

/** Per-node funnel for the whole sequence — surfaces follow-up emails,
 *  in-thread replies, waits, and every LinkedIn step, not just the first
 *  email.  Only shown when the sequence actually has more than one node. */
function SequencePerformance({ id }) {
  const { data, isLoading } = useQuery({
    queryKey: ['sequence-analytics', id],
    queryFn: () => getSequenceAnalytics(id),
    refetchInterval: 30000,
  });

  if (isLoading || !data) return null;
  const nodes = data.per_node ?? [];
  // Nothing secondary to report on a plain single-email sequence — the
  // email metrics above already cover it.
  if (nodes.length <= 1) return null;

  const summary = [
    { label: 'Active', value: data.active, color: 'text-emerald-700' },
    { label: 'Completed', value: data.completed, color: 'text-brand-700' },
    { label: 'Pending', value: data.pending, color: 'text-amber-600' },
    { label: 'Halted', value: data.halted, color: 'text-red-600' },
  ];

  return (
    <div
      data-testid="sequence-performance"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden mb-4"
    >
      <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Sequence performance</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Every step — follow-ups, replies, and LinkedIn — not just the first email.
          </p>
        </div>
        <div className="flex gap-4">
          {summary.map(({ label, value, color }) => (
            <div key={label} className="text-center">
              <div className={`text-lg font-bold ${color}`}>{value ?? 0}</div>
              <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
            </div>
          ))}
        </div>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-50">
            <th className="px-4 py-2 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider">Step</th>
            <th className="px-4 py-2 text-right text-xs font-semibold text-slate-500 uppercase tracking-wider">Sent</th>
            <th className="px-4 py-2 text-right text-xs font-semibold text-slate-500 uppercase tracking-wider">Skipped</th>
            <th className="px-4 py-2 text-right text-xs font-semibold text-slate-500 uppercase tracking-wider">Failed</th>
            <th className="px-4 py-2 text-right text-xs font-semibold text-slate-500 uppercase tracking-wider">Here now</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n, i) => (
            <tr key={n.node_id} data-testid="seq-node-row" className="border-t border-slate-100 hover:bg-slate-50">
              <td className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${SEQ_KIND_COLORS[n.kind] ?? 'bg-slate-100 text-slate-600'}`}>
                    {SEQ_KIND_LABELS[n.kind] ?? n.kind}
                  </span>
                  <span className="text-slate-700">{stepName(n, i)}</span>
                  {n.is_entry && (
                    <span className="text-[10px] text-slate-400 uppercase tracking-wide">start</span>
                  )}
                </div>
              </td>
              <td className="px-4 py-2.5 text-right tabular font-medium text-emerald-700">{n.sent}</td>
              <td className="px-4 py-2.5 text-right tabular text-slate-500">{n.skipped}</td>
              <td className="px-4 py-2.5 text-right tabular text-red-600">{n.failed}</td>
              <td className="px-4 py-2.5 text-right tabular text-brand-700 align-top">
                {n.currently_here}
                {n.here_already_sent > 0 && (
                  <div className="text-[10px] text-slate-400 font-normal" data-testid="seq-here-breakdown">
                    {n.here_already_sent} already sent · {n.currently_here - n.here_already_sent} to send
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-4 py-2.5 text-xs text-slate-400 border-t border-slate-100">
        <strong className="text-slate-500 font-medium">Sent</strong> counts unique recipients per step.
        Leads marked <em>"already sent"</em> under <em>Here now</em> have received this step before —
        the send-once guard makes them <strong className="text-slate-500 font-medium">skip and advance</strong>,
        so they are never re-sent; only <em>"to send"</em> leads will receive it.
      </p>
    </div>
  );
}

export function AnalyticsContent({ id, includeLeadTable = true }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['analytics', id],
    queryFn: () => getAnalytics(id),
    refetchInterval: 30000,
  });

  if (isLoading) return <div className="text-sm text-slate-500">Loading analytics…</div>;
  if (error) return <div className="text-sm text-red-600">Failed to load analytics</div>;
  if (!data) return null;

  const replyTracking = data.reply_tracking_enabled;

  const topRow = [
    { label: 'Sent', value: data.overview.sent },
    { label: 'Delivered', value: data.overview.delivered },
    { label: 'Open rate', value: pct(data.rates.open_rate) },
    {
      label: 'Click rate',
      value: data.click_tracking_enabled === false ? 'Not tracked' : pct(data.rates.click_rate),
      tooltip: data.click_tracking_enabled === false ? 'Click tracking is turned off in Brevo' : null,
    },
  ];

  const secondRow = [
    {
      label: 'Reply rate',
      value: replyTracking ? pct(data.rates.reply_rate) : '--',
      tooltip: replyTracking ? null : 'Connect an inbox to track replies',
    },
    { label: 'Bounce rate', value: pct(data.rates.bounce_rate) },
    { label: 'Spam complaints', value: data.overview.spam_complaints },
    { label: 'Unsubscribed', value: data.overview.unsubscribed },
  ];

  return (
    <div>
      <FailedLeadsBanner id={id} />

      <MetricsGrid metrics={topRow} />
      <MetricsGrid metrics={secondRow} />

      {!replyTracking && (
        <div data-testid="reply-tracking-banner" className="flex items-center gap-3 p-4 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800 mb-4">
          Reply tracking is not configured for this campaign.{' '}
          <a href="/settings" className="text-brand-600 hover:underline">Connect an inbox</a>{' '}
          in Settings to track replies on future campaigns.
        </div>
      )}

      <Timeline points={data.timeline} replyTrackingEnabled={replyTracking} />

      <SequencePerformance id={id} />

      <ReputationCard score={data.sender_reputation_score} rates={data.rates} />

      <QualityBreakdown items={data.research_quality_breakdown} />

      <BestSubjects subjects={data.best_subject_lines} />

      {includeLeadTable && (
        <div className="mt-6">
          <h2 className="text-xl font-semibold text-slate-900 mb-4">Leads</h2>
          <LeadTable campaignId={id} replyTrackingEnabled={replyTracking} />
        </div>
      )}
    </div>
  );
}


export default function Analytics() {
  const { id } = useParams();
  return (
    <div className="p-8 max-w-[1100px] mx-auto">
      <h1 className="text-2xl font-bold text-slate-900 mb-6">Analytics</h1>
      <AnalyticsContent id={id} />
    </div>
  );
}
