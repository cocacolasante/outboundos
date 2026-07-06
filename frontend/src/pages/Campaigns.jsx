import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteCampaign,
  listCampaigns,
  pauseCampaign,
  resumeCampaign,
} from '../api/campaigns.js';
import { useToast } from '../components/Toast.jsx';
import { PageHeader } from '../components/ui.jsx';

const STATUS_CLASSES = {
  draft:      'bg-slate-100 text-slate-600',
  previewing: 'bg-yellow-100 text-yellow-700',
  approved:   'bg-brand-100 text-brand-700',
  running:    'bg-emerald-100 text-emerald-700',
  paused:     'bg-amber-100 text-amber-700',
  complete:   'bg-indigo-100 text-indigo-700',
  failed:     'bg-red-100 text-red-600',
};


function StatusBadge({ status }) {
  const cls = STATUS_CLASSES[status] || STATUS_CLASSES.draft;
  return (
    <span
      data-testid="status-badge"
      data-status={status}
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium capitalize ${cls}`}
    >
      {status}
    </span>
  );
}


function pct(v) {
  if (v == null) return '--';
  return `${(v * 100).toFixed(1)}%`;
}


function CampaignCard({ campaign }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();

  const pauseMutation = useMutation({
    mutationFn: () => pauseCampaign(campaign.id),
    onSuccess: () => {
      toast.success(`"${campaign.name}" paused`);
      queryClient.invalidateQueries({ queryKey: ['campaigns'] });
    },
    onError: (e) => toast.error(e?.response?.data?.detail || e.message || 'Pause failed'),
  });
  const resumeMutation = useMutation({
    mutationFn: () => resumeCampaign(campaign.id),
    onSuccess: () => {
      toast.success(`"${campaign.name}" resumed`);
      queryClient.invalidateQueries({ queryKey: ['campaigns'] });
    },
    onError: (e) => toast.error(e?.response?.data?.detail || e.message || 'Resume failed'),
  });
  const deleteMutation = useMutation({
    mutationFn: () => deleteCampaign(campaign.id),
    onSuccess: () => {
      toast.success(`"${campaign.name}" deleted`);
      queryClient.invalidateQueries({ queryKey: ['campaigns'] });
    },
    onError: (e) => toast.error(e?.response?.data?.detail || e.message || 'Delete failed'),
  });

  const total = campaign.lead_counts?.total ?? 0;
  const sent = campaign.lead_counts?.sent ?? 0;
  const progress = total > 0 ? Math.min(100, (sent / total) * 100) : 0;
  const replyTracking = campaign.connected_account_configured;

  return (
    <div data-testid="campaign-card" data-campaign-id={campaign.id} className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
      <div className="flex justify-between items-start gap-3 mb-3">
        <h3 className="m-0 text-base font-semibold text-slate-900">{campaign.name}</h3>
        <StatusBadge status={campaign.status} />
      </div>

      <div className="text-xs text-slate-500 mb-1">
        {sent} of {total} sent
      </div>
      <div className="h-2 bg-slate-200 rounded-full overflow-hidden mb-4">
        <div
          data-testid="progress-bar"
          className="h-full bg-brand-600 rounded-full transition-all"
          style={{ width: `${progress}%` }}
        />
      </div>

      <div className="flex gap-4 mb-3">
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Open</div>
          <div className="text-sm font-semibold text-slate-900">{pct(campaign.stats?.open_rate)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Click</div>
          {campaign.stats?.click_tracking_enabled === false ? (
            <div className="text-sm font-medium text-slate-400" title="Click tracking is turned off in Brevo">
              Not tracked
            </div>
          ) : (
            <div className="text-sm font-semibold text-slate-900">{pct(campaign.stats?.click_rate)}</div>
          )}
        </div>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Reply</div>
          <div
            className="text-sm font-semibold text-slate-900"
            data-testid="reply-rate"
            title={replyTracking ? '' : 'Connect an inbox to track replies'}
          >
            {replyTracking ? pct(campaign.stats?.reply_rate) : '--'}
          </div>
        </div>
      </div>

      <div className="text-xs text-slate-400 mb-4">
        Created {new Date(campaign.created_at).toLocaleDateString()}
      </div>

      <div className="flex gap-2 flex-wrap">
        <button
          type="button"
          onClick={() => navigate(`/campaigns/${campaign.id}`)}
          className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors"
        >
          View
        </button>
        {campaign.status === 'running' && (
          <button
            type="button"
            onClick={() => pauseMutation.mutate()}
            disabled={pauseMutation.isPending}
            className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors disabled:opacity-50"
            data-testid="pause-button"
          >
            Pause
          </button>
        )}
        {campaign.status === 'paused' && (
          <button
            type="button"
            onClick={() => resumeMutation.mutate()}
            disabled={resumeMutation.isPending}
            className="px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-md transition-colors disabled:opacity-50"
            data-testid="resume-button"
          >
            Resume
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            if (confirm(`Delete campaign "${campaign.name}"? This cannot be undone.`)) {
              deleteMutation.mutate();
            }
          }}
          disabled={deleteMutation.isPending}
          className="px-3 py-1.5 bg-white hover:bg-red-50 text-red-600 text-xs font-medium border border-red-200 rounded-md transition-colors disabled:opacity-50"
          data-testid="delete-button"
        >
          Delete
        </button>
      </div>
    </div>
  );
}


function Skeleton() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4" data-testid="loading-skeleton">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 opacity-40 animate-pulse">
          <div className="bg-slate-200 h-4 w-3/5 rounded mb-4" />
          <div className="bg-slate-200 h-3 w-4/5 rounded mb-3" />
          <div className="bg-slate-200 h-2 w-full rounded" />
        </div>
      ))}
    </div>
  );
}


function EmptyState() {
  return (
    <div data-testid="empty-state" className="text-center py-16 text-slate-500">
      <div className="w-16 h-16 mx-auto mb-4 bg-slate-100 rounded-full flex items-center justify-center">
        <svg className="w-8 h-8 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
      </div>
      <h2 className="text-lg font-semibold text-slate-900 mb-2">No campaigns yet</h2>
      <p className="text-slate-500 mb-6 max-w-sm mx-auto">
        Create your first campaign to start sending personalized cold emails.
      </p>
      <Link
        to="/campaigns/new"
        className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors no-underline"
      >
        + Create your first campaign
      </Link>
    </div>
  );
}


export default function Campaigns() {
  const { data: campaigns, isLoading, error } = useQuery({
    queryKey: ['campaigns'],
    queryFn: listCampaigns,
  });

  return (
    <div className="p-8 max-w-[1100px] mx-auto">
      <PageHeader
        title="Campaigns"
        actions={(
          <Link
            to="/campaigns/new"
            className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors duration-fast no-underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1"
          >
            + New campaign
          </Link>
        )}
      />

      {isLoading && <Skeleton />}
      {error && (
        <div className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800">
          Failed to load campaigns: {String(error?.message)}
        </div>
      )}
      {!isLoading && !error && campaigns?.length === 0 && <EmptyState />}
      {!isLoading && !error && campaigns?.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {campaigns.map((c) => (
            <CampaignCard key={c.id} campaign={c} />
          ))}
        </div>
      )}
    </div>
  );
}
