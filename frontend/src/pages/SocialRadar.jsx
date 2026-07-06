import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  cleanupStalePosts,
  createSearch,
  deleteSearch,
  estimateRunCost,
  getSearch,
  listOpportunities,
  listSearches,
  previewExpand,
  requalifyAll,
  runSearch,
  updateOpportunity,
  updateSearch,
} from '../api/socialRadar.js';
import { useToast } from '../components/Toast.jsx';
import { Tabs } from '../components/ui.jsx';
import { Skeleton } from '../components/states.jsx';

/** Vertical stack of card-shaped skeletons for the opportunity feeds. */
function FeedSkeleton({ count = 3, testId = 'feed-loading' }) {
  return (
    <div className="space-y-3" data-testid={testId}>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="bg-white rounded-card border border-slate-200 p-4">
          <Skeleton className="h-4 w-1/3 mb-3" />
          <Skeleton className="h-3 w-full mb-2" />
          <Skeleton className="h-3 w-4/5" />
        </div>
      ))}
    </div>
  );
}

const CATEGORY_OPTIONS = [
  { value: '', label: 'All categories' },
  { value: 'ucaas_phone', label: 'UCaaS / phone' },
  { value: 'internet', label: 'Internet / connectivity' },
  { value: 'cybersecurity', label: 'Cybersecurity' },
  { value: 'cloud', label: 'Cloud' },
  { value: 'msp', label: 'MSP / vendor mgmt' },
  { value: 'crm_software', label: 'CRM / software' },
  { value: 'nonprofit_tech', label: 'Nonprofit tech' },
  { value: 'general_advisory', label: 'General advisory' },
  { value: 'not_relevant', label: 'Not relevant' },
];

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'new', label: 'New' },
  { value: 'saved', label: 'Saved' },
  { value: 'commented', label: 'Commented' },
  { value: 'connected', label: 'Connected' },
  { value: 'replied', label: 'Replied' },
  { value: 'not_relevant', label: 'Not relevant' },
  { value: 'archived', label: 'Archived' },
];

const FREQUENCY_OPTIONS = [
  { value: 'manual', label: 'Manual only' },
  { value: 'every_6h', label: 'Every 6 hours' },
  { value: 'every_12h', label: 'Every 12 hours' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
];

const STATUS_PILL = {
  new: 'bg-brand-100 text-brand-700',
  saved: 'bg-amber-100 text-amber-700',
  commented: 'bg-emerald-100 text-emerald-700',
  connected: 'bg-emerald-100 text-emerald-700',
  replied: 'bg-violet-100 text-violet-700',
  not_relevant: 'bg-slate-100 text-slate-500',
  archived: 'bg-slate-100 text-slate-400',
};

const SEARCH_STATUS_PILL = {
  active: 'bg-emerald-100 text-emerald-700',
  paused: 'bg-amber-100 text-amber-700',
  archived: 'bg-slate-100 text-slate-500',
};

function StatusPill({ value, map = STATUS_PILL }) {
  if (!value) return <span className="text-slate-400">—</span>;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${map[value] || 'bg-slate-100 text-slate-700'}`}>
      {value.replace(/_/g, ' ')}
    </span>
  );
}

function ScoreBadge({ score }) {
  let cls = 'bg-slate-100 text-slate-700';
  if (score >= 7) cls = 'bg-emerald-100 text-emerald-700';
  else if (score >= 4) cls = 'bg-amber-100 text-amber-700';
  else if (score >= 1) cls = 'bg-red-100 text-red-700';
  return (
    <span
      className={`inline-flex items-center justify-center w-9 h-9 rounded-full text-sm font-bold ${cls}`}
      data-testid="score-badge"
    >
      {score}
    </span>
  );
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export default function SocialRadar() {
  const [tab, setTab] = useState('feed');
  const [editing, setEditing] = useState(null); // null | "new" | searchSummary
  // When set, the page enters "search detail" mode and the Feed/Searches
  // tabs are hidden until the user clicks Back.
  const [detailId, setDetailId] = useState(null);

  if (detailId) {
    return (
      <div className="p-6 max-w-7xl mx-auto">
        <SearchDetailView
          searchId={detailId}
          onBack={() => setDetailId(null)}
          onEdit={(s) => setEditing(s)}
        />
        {editing !== null && (
          <SearchEditorModal
            search={editing === 'new' ? null : editing}
            onClose={() => setEditing(null)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex justify-between items-start mb-1">
        <h1 className="text-2xl font-bold text-slate-900">Social Radar</h1>
        <button
          type="button"
          onClick={() => setEditing('new')}
          data-testid="new-search-btn"
          className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700"
        >
          + New search
        </button>
      </div>
      <p className="text-sm text-slate-500 mb-4">
        Plain-English topic → AI expands to 20-ish LinkedIn search phrases →
        scored intent feed.  All comments / connects stay manual — the system
        only drafts suggestions.
      </p>

      <Tabs
        testId="tab"
        className="mb-4"
        active={tab}
        onChange={setTab}
        tabs={[{ key: 'feed', label: 'Feed' }, { key: 'searches', label: 'Searches' }]}
      />

      {tab === 'feed' && <FeedTab />}
      {tab === 'searches' && (
        <SearchesTab
          onView={(s) => setDetailId(s.id)}
          onEdit={(s) => setEditing(s)}
        />
      )}

      {editing !== null && (
        <SearchEditorModal
          search={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}



// ===========================================================================
// Feed tab
// ===========================================================================

function FeedTab() {
  const [page, setPage] = useState(1);
  const [searchId, setSearchId] = useState('');
  const [category, setCategory] = useState('');
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [minScore, setMinScore] = useState('');
  // Sort: default matches the backend's default (score DESC).  asc/desc
  // toggle is independent of the sort key.
  const [sortBy, setSortBy] = useState('score');
  const [sortOrder, setSortOrder] = useState('desc');

  const { data: searchesData } = useQuery({
    queryKey: ['social-radar-searches-min'],
    queryFn: () => listSearches({ page_size: 200 }),
  });
  const searches = searchesData?.items ?? [];

  const params = { page, page_size: 25 };
  if (searchId) params.search_id = searchId;
  if (category) params.category = category;
  if (status) params.status = status;
  if (source) params.source = source;
  if (minScore) params.min_score = Number(minScore);
  // Always send sort params so the server-side default isn't a surprise
  // (asc/desc on score is the only thing we send by default).
  params.sort_by = sortBy;
  params.sort_order = sortOrder;

  const { data: feed, isLoading } = useQuery({
    queryKey: ['social-radar-opps', params],
    queryFn: () => listOpportunities(params),
    keepPreviousData: true,
  });

  return (
    <div data-testid="feed-tab">
      {/* Filters */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4 mb-4 flex flex-wrap items-center gap-3">
        <select
          value={searchId}
          onChange={(e) => { setSearchId(e.target.value); setPage(1); }}
          aria-label="Filter by search"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
        >
          <option value="">All searches</option>
          {searches.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <select
          value={category}
          onChange={(e) => { setCategory(e.target.value); setPage(1); }}
          aria-label="Filter by category"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
        >
          {CATEGORY_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1); }}
          aria-label="Filter by status"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select
          value={source}
          onChange={(e) => { setSource(e.target.value); setPage(1); }}
          aria-label="Filter by source"
          data-testid="source-filter"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
        >
          <option value="">All sources</option>
          <option value="linkedin">LinkedIn</option>
          <option value="reddit">Reddit</option>
          <option value="twitter">X / Twitter</option>
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Min score
          <input
            type="number" min={1} max={10}
            value={minScore}
            onChange={(e) => { setMinScore(e.target.value); setPage(1); }}
            data-testid="min-score-filter"
            className="w-16 px-2 py-1 border border-slate-300 rounded text-sm"
          />
        </label>

        {/* Sort controls — visually separated to the right via ml-auto */}
        <div className="flex items-center gap-2 ml-auto text-sm text-slate-600">
          <span>Sort by</span>
          <select
            value={sortBy}
            onChange={(e) => { setSortBy(e.target.value); setPage(1); }}
            aria-label="Sort by"
            data-testid="sort-by"
            className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
          >
            <option value="score">Score</option>
            <option value="discovered_at">Discovered date</option>
            <option value="updated_at">Last updated</option>
          </select>
          <button
            type="button"
            onClick={() => { setSortOrder((o) => (o === 'desc' ? 'asc' : 'desc')); setPage(1); }}
            aria-label={`Sort order: ${sortOrder === 'desc' ? 'descending' : 'ascending'}`}
            data-testid="sort-order"
            title={sortOrder === 'desc' ? 'Highest first — click to flip' : 'Lowest first — click to flip'}
            className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white hover:bg-slate-50"
          >
            {sortOrder === 'desc' ? '↓ Desc' : '↑ Asc'}
          </button>
        </div>
      </div>

      {/* Cards */}
      {isLoading ? (
        <FeedSkeleton testId="feed-loading" />
      ) : (feed?.items ?? []).length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-8 text-center text-slate-500" data-testid="feed-empty">
          No opportunities yet. Create a search and click <strong>Run now</strong>.
        </div>
      ) : (
        <div className="space-y-3">
          {feed.items.map((o) => <OpportunityCard key={o.id} opp={o} />)}
        </div>
      )}

      {feed && feed.total_pages > 1 && (
        <div className="flex justify-between items-center mt-4 text-sm text-slate-600">
          <div>Page {feed.page} of {feed.total_pages} · {feed.total} total</div>
          <div className="flex gap-2">
            <button type="button" disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="px-3 py-1.5 border border-slate-300 rounded-lg disabled:opacity-50">
              Previous
            </button>
            <button type="button" disabled={page >= (feed?.total_pages ?? 1)}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1.5 border border-slate-300 rounded-lg disabled:opacity-50">
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


function OpportunityCard({ opp }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);

  const statusMut = useMutation({
    mutationFn: (next) => updateOpportunity(opp.id, { status: next }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-opps'] });
      toast.success('Status updated');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to update'),
  });

  const copy = async (text, label) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${label} copied`);
    } catch {
      toast.error('Clipboard unavailable');
    }
  };

  const post = opp.post;
  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-5" data-testid={`opportunity-${opp.id}`}>
      <div className="flex justify-between items-start gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <ScoreBadge score={opp.score} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-semibold text-slate-900">{post.author_name || 'Unknown author'}</span>
              {post.author_headline && (
                <span className="text-sm text-slate-500 truncate">{post.author_headline}</span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <StatusPill value={opp.category} map={{}} />
              {opp.buying_signal && (
                <span className="text-xs font-medium text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded">
                  buying signal
                </span>
              )}
              <StatusPill value={opp.status} />
              <span className="text-xs text-slate-400">· {opp.search_name || 'unknown search'}</span>
              <span className="text-xs text-slate-400">· {fmtDate(post.discovered_at)}</span>
            </div>
          </div>
        </div>
        <select
          value={opp.status}
          onChange={(e) => statusMut.mutate(e.target.value)}
          data-testid={`opp-status-${opp.id}`}
          className="px-2 py-1 border border-slate-300 rounded text-sm bg-white shrink-0"
        >
          {STATUS_OPTIONS.filter((o) => o.value).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      <p className="text-sm text-slate-700 mt-3 whitespace-pre-wrap" data-testid="post-text">
        {expanded || post.post_text.length <= 280 ? post.post_text : `${post.post_text.slice(0, 280)}…`}
      </p>
      {post.post_text.length > 280 && (
        <button type="button" onClick={() => setExpanded((v) => !v)} className="text-xs text-brand-600">
          {expanded ? 'show less' : 'show more'}
        </button>
      )}

      {(opp.pain_summary || opp.qualification_reason) && (
        <div className="mt-3 text-xs text-slate-600 space-y-1">
          {opp.pain_summary && <div><span className="font-medium">Pain:</span> {opp.pain_summary}</div>}
          {opp.qualification_reason && <div><span className="font-medium">Why:</span> {opp.qualification_reason}</div>}
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {opp.suggested_comment && (
          <button type="button"
            onClick={() => copy(opp.suggested_comment, 'Suggested comment')}
            data-testid={`copy-comment-${opp.id}`}
            className="px-3 py-1.5 text-xs border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
            Copy comment
          </button>
        )}
        {opp.suggested_connection_request && (
          <button type="button"
            onClick={() => copy(opp.suggested_connection_request, 'Connection request')}
            data-testid={`copy-connect-${opp.id}`}
            className="px-3 py-1.5 text-xs border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
            Copy connect msg
          </button>
        )}
        {opp.suggested_follow_up && (
          <button type="button"
            onClick={() => copy(opp.suggested_follow_up, 'Follow-up DM')}
            className="px-3 py-1.5 text-xs border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
            Copy follow-up
          </button>
        )}
        <a href={post.post_url} target="_blank" rel="noreferrer"
           className="px-3 py-1.5 text-xs border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
          Open post ↗
        </a>
      </div>
    </div>
  );
}


// ===========================================================================
// Searches tab
// ===========================================================================

function SearchesTab({ onView, onEdit }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data, isLoading } = useQuery({
    queryKey: ['social-radar-searches'],
    queryFn: () => listSearches({ page_size: 200 }),
  });

  const runMut = useMutation({
    mutationFn: (id) => runSearch(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      toast.success('Search queued');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to run'),
  });

  const deleteMut = useMutation({
    mutationFn: (id) => deleteSearch(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-opps'] });
      toast.success('Search deleted');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  const rows = data?.items ?? [];

  return (
    <div data-testid="searches-tab">
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <table className="w-full text-sm" data-testid="searches-table">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase">
              <th className="px-4 py-3 text-left">Name</th>
              <th className="px-4 py-3 text-left">Topic</th>
              <th className="px-4 py-3 text-left">Frequency</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Posts</th>
              <th className="px-4 py-3 text-left">Opps</th>
              <th className="px-4 py-3 text-left">Last run</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              [0, 1, 2].map((i) => (
                <tr key={`sk-${i}`} className="border-b border-slate-100">
                  {Array.from({ length: 8 }).map((__, c) => (
                    <td key={c} className="px-4 py-3"><Skeleton className="h-4 w-full" /></td>
                  ))}
                </tr>
              ))
            ) : rows.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-6 text-center text-slate-500" data-testid="searches-empty">
                No searches yet. Click <strong>+ New search</strong> to start.
              </td></tr>
            ) : (
              rows.map((s) => (
                <tr
                  key={s.id}
                  onClick={() => onView(s)}
                  data-testid={`search-row-${s.id}`}
                  className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer"
                >
                  <td className="px-4 py-3">
                    {/* The name remains a focusable button for keyboard
                        users + a testable target, but it shares the same
                        onClick as the row.  stopPropagation isn't needed
                        because both targets call the same handler. */}
                    <button type="button" onClick={() => onView(s)}
                      data-testid={`view-${s.id}`}
                      className="text-slate-900 font-medium hover:text-brand-700 text-left">
                      {s.name}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-slate-600 max-w-[280px] truncate" title={s.topic}>{s.topic}</td>
                  <td className="px-4 py-3 text-slate-600">{s.frequency}</td>
                  <td className="px-4 py-3"><StatusPill value={s.status} map={SEARCH_STATUS_PILL} /></td>
                  <td className="px-4 py-3 text-slate-600">{s.post_count}</td>
                  <td className="px-4 py-3 text-slate-600">{s.opportunity_count}</td>
                  <td className="px-4 py-3 text-xs text-slate-500" title={s.last_run_status || ''}>
                    {s.last_run_at ? fmtDate(s.last_run_at) : '—'}
                  </td>
                  {/* Stop click from bubbling so action buttons don't ALSO
                      open the detail view. */}
                  <td className="px-4 py-3 text-right whitespace-nowrap"
                      onClick={(e) => e.stopPropagation()}>
                    <button type="button" onClick={() => runMut.mutate(s.id)}
                      disabled={runMut.isPending || s.last_run_status === 'running'}
                      data-testid={`run-${s.id}`}
                      className="text-xs text-brand-600 hover:text-brand-700 mr-3 disabled:opacity-50">
                      Run now
                    </button>
                    <button type="button" onClick={() => onEdit(s)}
                      data-testid={`edit-${s.id}`}
                      className="text-xs text-slate-600 hover:text-slate-800 mr-3">
                      Edit
                    </button>
                    <button type="button"
                      onClick={() => { if (confirm(`Delete "${s.name}"? Posts + opportunities are also deleted.`)) deleteMut.mutate(s.id); }}
                      className="text-xs text-red-600 hover:text-red-700">
                      Delete
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}


// ===========================================================================
// Search detail view — activity + opportunities for ONE search
// ===========================================================================

function SearchDetailView({ searchId, onBack, onEdit }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [statusFilter, setStatusFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [sortBy, setSortBy] = useState('score');
  const [sortOrder, setSortOrder] = useState('desc');

  // Pre-run cost estimate.  Tied to a coarse cache key so it doesn't
  // refetch on every render; recomputes when settings change because
  // we invalidate the search-detail query, which is its prerequisite.
  const { data: estimate } = useQuery({
    queryKey: ['social-radar-estimate', searchId],
    queryFn: () => estimateRunCost(searchId),
    staleTime: 60000,
  });

  // Detail (with expanded_queries + counts) is refetched while a run is
  // in flight so the activity card stays live.
  const { data: search, isLoading: searchLoading } = useQuery({
    queryKey: ['social-radar-search-detail', searchId],
    queryFn: () => getSearch(searchId),
    refetchInterval: (q) =>
      q.state.data?.last_run_status === 'running' ? 5000 : false,
  });

  const oppParams = { search_id: searchId, page_size: 50 };
  if (statusFilter) oppParams.status = statusFilter;
  if (sourceFilter) oppParams.source = sourceFilter;
  oppParams.sort_by = sortBy;
  oppParams.sort_order = sortOrder;

  const { data: opps, isLoading: oppsLoading } = useQuery({
    queryKey: ['social-radar-opps', oppParams],
    queryFn: () => listOpportunities(oppParams),
    keepPreviousData: true,
    refetchInterval: (q) =>
      search?.last_run_status === 'running' ? 5000 : false,
  });

  const runMut = useMutation({
    mutationFn: () => runSearch(searchId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-search-detail', searchId] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      toast.success('Search queued');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to run'),
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteSearch(searchId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-opps'] });
      toast.success('Search deleted');
      onBack();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  const requalifyMut = useMutation({
    mutationFn: () => requalifyAll(searchId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-opps'] });
      toast.success(
        `Re-qualifying ${data.enqueued} posts in ${data.batches} batches… `
        + 'opportunities will refresh as each batch finishes.',
      );
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Re-qualify failed'),
  });

  const cleanupMut = useMutation({
    mutationFn: () => cleanupStalePosts(searchId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['social-radar-search-detail', searchId] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-opps'] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      toast.success(`Cleaned up ${data.deleted} stale post${data.deleted === 1 ? '' : 's'}`);
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Cleanup failed'),
  });

  if (searchLoading || !search) {
    return (
      <div>
        <BackHeader onBack={onBack} />
        <div className="mt-4" data-testid="search-detail-loading">
          <Skeleton className="h-6 w-48 mb-3" />
          <Skeleton className="h-4 w-72 mb-6" />
          <FeedSkeleton count={2} testId="search-detail-skeleton" />
        </div>
      </div>
    );
  }

  return (
    <div data-testid="search-detail">
      <BackHeader onBack={onBack} />

      {/* Title row */}
      <div className="flex justify-between items-start mb-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-2xl font-bold text-slate-900 m-0 truncate">{search.name}</h1>
            <StatusPill value={search.status} map={SEARCH_STATUS_PILL} />
          </div>
          <p className="text-sm text-slate-600 m-0">{search.topic}</p>
          <p className="text-xs text-slate-500 mt-1">
            {search.niche ? <>niche: <strong>{search.niche}</strong> · </> : null}
            {search.geography ? <>geography: <strong>{search.geography}</strong> · </> : null}
            frequency: <strong>{search.frequency}</strong>
            {' · '}lookback: <strong data-testid="detail-lookback">{search.max_post_age_days} days</strong>
            {' · '}sources: <strong data-testid="detail-sources">
              {(search.sources || []).map((s) => s).join(', ') || '—'}
            </strong>
            {(search.linkedin_profile_watchlist?.length ?? 0) > 0 && (
              <>
                {' · '}watchlist: <strong data-testid="detail-watchlist-count">
                  {search.linkedin_profile_watchlist.length} profile{search.linkedin_profile_watchlist.length === 1 ? '' : 's'}
                </strong>
              </>
            )}
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button type="button"
            onClick={() => runMut.mutate()}
            disabled={runMut.isPending || search.last_run_status === 'running'}
            data-testid="detail-run-btn"
            title={estimate ? `Estimated cost: $${estimate.total_cost_usd.toFixed(2)} (cap $${estimate.max_run_cost_usd.toFixed(2)})` : undefined}
            className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
            {search.last_run_status === 'running'
              ? 'Running…'
              : (estimate
                ? `Run now (~$${estimate.total_cost_usd.toFixed(2)})`
                : 'Run now')}
          </button>
          <button type="button"
            onClick={() => onEdit(search)}
            data-testid="detail-edit-btn"
            className="px-3 py-1.5 text-sm border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
            Edit
          </button>
          <button type="button"
            onClick={() => {
              const n = search.post_count || 0;
              if (n === 0) {
                toast.info('No posts to re-qualify yet — run a discovery first.');
                return;
              }
              if (confirm(`Re-score all ${n} posts for this search under the current qualifier prompt? Your saved/commented status + notes are preserved. ~$${(n * 0.001).toFixed(2)} estimated.`)) {
                requalifyMut.mutate();
              }
            }}
            disabled={requalifyMut.isPending}
            data-testid="detail-requalify-btn"
            className="px-3 py-1.5 text-sm border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50 disabled:opacity-50">
            {requalifyMut.isPending ? 'Queuing…' : 'Re-score all'}
          </button>
          <button type="button"
            onClick={() => {
              if (confirm(`Delete posts for "${search.name}" older than ${search.max_post_age_days} days (or with no date)?  Their opportunities are also deleted.  Anything you've marked Saved / Commented will be lost if it's a stale post.`)) {
                cleanupMut.mutate();
              }
            }}
            disabled={cleanupMut.isPending}
            data-testid="detail-cleanup-btn"
            className="px-3 py-1.5 text-sm border border-amber-300 rounded-lg text-amber-700 hover:bg-amber-50 disabled:opacity-50">
            {cleanupMut.isPending ? 'Cleaning…' : 'Clean up stale'}
          </button>
          <button type="button"
            onClick={() => {
              if (confirm(`Delete "${search.name}"? Posts + opportunities are also deleted.`)) {
                deleteMut.mutate();
              }
            }}
            data-testid="detail-delete-btn"
            className="px-3 py-1.5 text-sm border border-red-300 rounded-lg text-red-600 hover:bg-red-50">
            Delete
          </button>
        </div>
      </div>

      {/* Activity card */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-5 mb-4" data-testid="detail-activity">
        <h2 className="text-base font-semibold text-slate-900 mb-3">Activity</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <Stat label="Last run" value={search.last_run_at ? fmtDate(search.last_run_at) : '—'} />
          <Stat
            label="Last status"
            value={
              <StatusPill
                value={search.last_run_status}
                map={LAST_RUN_PILL}
              />
            }
          />
          <Stat label="Posts found" value={search.post_count} />
          <Stat label="Opportunities" value={search.opportunity_count} />
        </div>
        {search.last_run_error && (
          <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2 mb-3" data-testid="detail-last-error">
            <strong>Last run error:</strong> {search.last_run_error}
          </div>
        )}
        {search.next_run_at && (
          <div className="text-xs text-slate-500 mb-3">
            Next scheduled run: <strong>{fmtDate(search.next_run_at)}</strong>
          </div>
        )}
        <div>
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
            Expanded queries ({search.expanded_query_count})
          </div>
          {search.expanded_queries?.length ? (
            <div className="flex flex-wrap gap-1.5" data-testid="detail-expanded-queries">
              {search.expanded_queries.map((q) => (
                <span key={q} className="px-2 py-0.5 bg-brand-50 text-brand-700 text-xs rounded">{q}</span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-slate-400 italic">
              No queries yet — the AI expansion is still running (or hasn't been kicked off).
            </div>
          )}
        </div>

        <PerQueryStatsTable stats={search.last_run_stats} />
      </div>

      {/* Opportunities card */}
      <div className="flex flex-wrap justify-between items-center gap-2 mb-3">
        <h2 className="text-base font-semibold text-slate-900 m-0">Opportunities found</h2>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            aria-label="Filter detail by status"
            data-testid="detail-status-filter"
            className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white"
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            aria-label="Filter detail by source"
            data-testid="detail-source-filter"
            className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white"
          >
            <option value="">All sources</option>
            <option value="linkedin">LinkedIn</option>
            <option value="reddit">Reddit</option>
            <option value="twitter">X / Twitter</option>
          </select>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            aria-label="Detail sort by"
            data-testid="detail-sort-by"
            className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white"
          >
            <option value="score">Sort: score</option>
            <option value="discovered_at">Sort: discovered</option>
            <option value="updated_at">Sort: updated</option>
          </select>
          <button
            type="button"
            onClick={() => setSortOrder((o) => (o === 'desc' ? 'asc' : 'desc'))}
            aria-label={`Detail sort order: ${sortOrder === 'desc' ? 'descending' : 'ascending'}`}
            data-testid="detail-sort-order"
            className="px-3 py-1.5 border border-slate-300 rounded-lg text-sm bg-white hover:bg-slate-50"
          >
            {sortOrder === 'desc' ? '↓ Desc' : '↑ Asc'}
          </button>
        </div>
      </div>
      {oppsLoading ? (
        <FeedSkeleton testId="detail-opps-loading" />
      ) : (opps?.items ?? []).length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-8 text-center text-slate-500" data-testid="detail-empty">
          No opportunities yet for this search. Click <strong>Run now</strong> above, then wait ~1 min.
        </div>
      ) : (
        <div className="space-y-3" data-testid="detail-opps-list">
          {opps.items.map((o) => <OpportunityCard key={o.id} opp={o} />)}
        </div>
      )}
    </div>
  );
}

const LAST_RUN_PILL = {
  pending: 'bg-slate-100 text-slate-700',
  running: 'bg-brand-100 text-brand-700',
  done: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700',
};

function PerQueryStatsTable({ stats }) {
  const [open, setOpen] = useState(false);
  const queries = stats?.queries;
  const summary = stats?.summary;
  if (!Array.isArray(queries) || queries.length === 0) return null;

  return (
    <div className="mt-4 border-t border-slate-200 pt-3" data-testid="detail-perquery-stats">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-xs font-semibold text-slate-500 uppercase tracking-wide hover:text-slate-700"
        data-testid="perquery-stats-toggle"
      >
        {open ? '▾' : '▸'} Per-query results
        {summary && (
          <span className="font-normal normal-case text-slate-500 ml-2">
            ({summary.total_kept || 0} kept · {summary.total_dropped_stale || 0} stale ·
            {' '}{summary.total_dropped_undated || 0} undated)
          </span>
        )}
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 border-b border-slate-200">
                <th className="text-left py-1 pr-2 font-medium">Source</th>
                <th className="text-left py-1 pr-2 font-medium">Phrase</th>
                <th className="text-right px-2 font-medium">Raw</th>
                <th className="text-right px-2 font-medium">Kept</th>
                <th className="text-right px-2 font-medium">New</th>
                <th className="text-right px-2 font-medium">Stale</th>
                <th className="text-right px-2 font-medium">Undated</th>
                <th className="text-right px-2 font-medium">Seen</th>
                <th className="text-right px-2 font-medium">Dup</th>
                <th className="text-right px-2 font-medium">Bad URL</th>
              </tr>
            </thead>
            <tbody>
              {queries.map((q, i) => (
                <tr key={i} className="border-b border-slate-100">
                  <td className="py-1 pr-2 text-slate-500">{q.source || 'linkedin'}</td>
                  <td className="py-1 pr-2 text-slate-700 max-w-[300px] truncate" title={q.query}>{q.query}</td>
                  <td className="text-right px-2 text-slate-600">{q.raw ?? 0}</td>
                  <td className="text-right px-2 font-medium text-emerald-700">{q.kept ?? 0}</td>
                  <td className="text-right px-2 text-slate-600">{q.upserted_new ?? 0}</td>
                  <td className="text-right px-2 text-amber-700">{q.dropped_stale ?? 0}</td>
                  <td className="text-right px-2 text-amber-700">{q.dropped_undated ?? 0}</td>
                  <td className="text-right px-2 text-slate-500">{q.dropped_excluded ?? 0}</td>
                  <td className="text-right px-2 text-slate-500">{q.dropped_duplicate ?? 0}</td>
                  <td className="text-right px-2 text-slate-500">{q.dropped_invalid_url ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-slate-500 mt-2">
            <strong>Raw</strong> = entries Anthropic returned · <strong>Kept</strong> = passed all
            filters · <strong>New</strong> = inserted (not already in the dedup set) ·
            <strong> Stale</strong>/<strong>Undated</strong> = blocked by the freshness gate ·
            <strong> Seen</strong> = already in this search · <strong>Dup</strong> = same URL
            twice in one response · <strong>Bad URL</strong> = didn't look like a LinkedIn post.
          </p>
        </div>
      )}
    </div>
  );
}


function BackHeader({ onBack }) {
  return (
    <button
      type="button"
      onClick={onBack}
      data-testid="detail-back-btn"
      className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800 mb-3"
    >
      ← Back to Social Radar
    </button>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">{label}</div>
      <div className="text-sm text-slate-900 font-medium">{value}</div>
    </div>
  );
}


// ===========================================================================
// Editor modal (create + edit)
// ===========================================================================

const ALL_SOURCES = [
  { value: 'linkedin', label: 'LinkedIn', help: 'Often low recall — LinkedIn blocks indexing' },
  { value: 'reddit', label: 'Reddit', help: 'Heavily indexed; best signal source' },
  { value: 'twitter', label: 'X / Twitter', help: 'Public tweets are well indexed' },
];

const DEFAULT_DRAFT = {
  name: '',
  topic: '',
  niche: '',
  geography: '',
  include_keywords: '',
  exclude_keywords: '',
  sources: ['linkedin', 'reddit', 'twitter'],
  frequency: 'manual',
  status: 'active',
  tone: 'helpful',
  sender_name: '',
  max_post_age_days: 30,
  max_queries_per_run: 20,
  max_posts_per_query: 30,
  max_qualified_per_run: 100,
  max_run_cost_usd: 1.0,
  linkedin_web_search_enabled: false,
  linkedin_crosslink_enabled: true,
  // Editable expanded-queries textarea: when this is populated (edit
  // mode), the save payload sends ``expanded_queries`` and the backend
  // skips re-expansion.  An empty string on a new search leaves the
  // queries to be auto-generated by ``expand_social_topic_task``.
  expanded_queries_text: '',
  // LinkedIn profile URLs the user wants the worker to monitor every
  // run.  Web search has terrible recall on LinkedIn so this is the
  // reliable channel — Unipile pulls posts directly per profile.
  linkedin_profile_watchlist_text: '',
};

function _toCsv(arr) {
  return Array.isArray(arr) ? arr.join(', ') : '';
}
function _fromCsv(s) {
  return (s || '').split(',').map((x) => x.trim()).filter(Boolean);
}

// LinkedIn profile URL recogniser used by the watchlist bulk-paste
// validator.  Permissive on purpose: real-world URLs copied from
// browsers carry trailing path segments (``/recent-activity/all/``,
// ``/details/skills/``), query strings (``?originalSubdomain=us``,
// ``?utm_source=share``), hash fragments (``#about``), and mobile
// subdomains (``mwlite.linkedin.com``).  All of these resolve to the
// same profile and the worker's slug-extractor (``_linkedin_slug``)
// treats them identically, so the validator MUST accept them too —
// otherwise URLs the user pasted from their browser get silently
// dropped on save and the watchlist looks empty on reopen.
//
// Rule: any http(s) URL whose path contains ``linkedin.com/in/<slug>``
// where slug is one or more characters that aren't ``/``, whitespace,
// ``?``, or ``#``.  Anything after the slug (subpath / query / fragment)
// is ignored — the canonical slug is what we dedupe on.
const _LI_PROFILE_URL_RE =
  /^https?:\/\/[^\s/]*linkedin\.com\/in\/[^\/\s?#]+/i;

// Slug-extractor — runs AFTER the validator passed, so we know the
// match is there.  Captures everything up to ``/``, ``?``, ``#``, or
// end-of-string.  Includes ``.`` in the character class because some
// legacy custom URLs contain a dot (e.g. ``jane.doe``).
const _LI_SLUG_RE = /linkedin\.com\/in\/([A-Za-z0-9._%-]+)/i;

/** Split a free-form blob (newline-, comma-, or whitespace-separated)
 *  into trimmed candidate URLs.  Used by the watchlist textarea so the
 *  user can paste an entire CSV column / spreadsheet row / chat dump
 *  and we'll sort it out. */
export function parseWatchlistEntries(text) {
  if (!text) return [];
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Classify watchlist entries → { valid, invalid }.  Dedupes valid
 *  entries by canonical slug (case-insensitive) so the same profile
 *  pasted in multiple URL flavours (subpath / query / fragment) collapses
 *  to one entry — and the user's first variant wins. */
export function classifyWatchlistEntries(text) {
  const entries = parseWatchlistEntries(text);
  const valid = [];
  const invalid = [];
  const seenSlugs = new Set();
  for (const raw of entries) {
    if (!_LI_PROFILE_URL_RE.test(raw)) {
      invalid.push(raw);
      continue;
    }
    const m = raw.match(_LI_SLUG_RE);
    const slug = (m?.[1] || '').toLowerCase();
    if (!slug || seenSlugs.has(slug)) continue;
    seenSlugs.add(slug);
    valid.push(raw);
  }
  return { valid, invalid };
}

function SearchEditorModal({ search, onClose }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const isNew = !search;

  // The summary shape (what the searches table passes) lacks
  // ``tone`` / ``sender_name`` / ``include_keywords`` / ``exclude_keywords`` /
  // ``max_*`` / ``expanded_queries`` / ``linkedin_profile_watchlist`` —
  // those are only in the full detail.  Fetch by id whenever we're
  // editing so the form pre-fills correctly regardless of which entry
  // point opened the modal (table row click vs. detail-view Edit button).
  const { data: fullSearch, isLoading: detailLoading } = useQuery({
    queryKey: ['social-radar-search-detail', search?.id],
    queryFn: () => getSearch(search.id),
    enabled: !!search?.id,
    // The detail page already loads this, so this should be a cache hit
    // when the user clicked through detail → Edit.
  });

  const _initial = (s) => (s ? {
    ...DEFAULT_DRAFT,
    name: s.name || '',
    topic: s.topic || '',
    niche: s.niche || '',
    geography: s.geography || '',
    include_keywords: _toCsv(s.include_keywords),
    exclude_keywords: _toCsv(s.exclude_keywords),
    sources: Array.isArray(s.sources) && s.sources.length
      ? s.sources : (s.source ? [s.source] : ['linkedin']),
    frequency: s.frequency || 'manual',
    status: s.status || 'active',
    tone: s.tone || 'helpful',
    sender_name: s.sender_name || '',
    max_post_age_days: s.max_post_age_days ?? 30,
    max_queries_per_run: s.max_queries_per_run ?? 20,
    max_posts_per_query: s.max_posts_per_query ?? 30,
    max_qualified_per_run: s.max_qualified_per_run ?? 100,
    max_run_cost_usd: s.max_run_cost_usd ?? 1.0,
    linkedin_web_search_enabled: !!s.linkedin_web_search_enabled,
    linkedin_crosslink_enabled: s.linkedin_crosslink_enabled !== false,
    expanded_queries_text: (s.expanded_queries || []).join('\n'),
    linkedin_profile_watchlist_text: (s.linkedin_profile_watchlist || []).join('\n'),
  } : DEFAULT_DRAFT);

  const [draft, setDraft] = useState(() => _initial(search));
  const [hydrated, setHydrated] = useState(false);

  // When the full detail arrives, overwrite the draft with the richer
  // shape — but only once (so user edits aren't clobbered on refetch).
  if (fullSearch && !hydrated) {
    setHydrated(true);
    setDraft(_initial(fullSearch));
  }

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [preview, setPreview] = useState(null); // null | string[]

  const update = (key, val) => setDraft((d) => ({ ...d, [key]: val }));

  const previewMut = useMutation({
    mutationFn: () => previewExpand({
      topic: draft.topic,
      niche: draft.niche || null,
      geography: draft.geography || null,
      include_keywords: _fromCsv(draft.include_keywords),
      exclude_keywords: _fromCsv(draft.exclude_keywords),
      max_queries: Number(draft.max_queries_per_run) || 20,
    }),
    onSuccess: (data) => {
      setPreview(data.queries || []);
      if (!data.queries?.length) toast.info('No queries returned — check API key + topic');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Preview failed'),
  });

  const saveMut = useMutation({
    mutationFn: () => {
      const payload = {
        name: draft.name,
        topic: draft.topic,
        niche: draft.niche || null,
        geography: draft.geography || null,
        include_keywords: _fromCsv(draft.include_keywords),
        exclude_keywords: _fromCsv(draft.exclude_keywords),
        frequency: draft.frequency,
        status: draft.status,
        tone: draft.tone || 'helpful',
        sender_name: draft.sender_name || null,
        sources: (draft.sources && draft.sources.length) ? draft.sources : ['linkedin'],
        // Bulk-paste-friendly: accept commas + any whitespace + dedupe
        // by slug.  Invalid lines are silently dropped on save (the
        // count pill below the textarea shows the breakdown live).
        linkedin_profile_watchlist:
          classifyWatchlistEntries(draft.linkedin_profile_watchlist_text || '').valid,
        linkedin_web_search_enabled: !!draft.linkedin_web_search_enabled,
        linkedin_crosslink_enabled: draft.linkedin_crosslink_enabled !== false,
        max_run_cost_usd: Number(draft.max_run_cost_usd) || 1.0,
        max_post_age_days: Number(draft.max_post_age_days) || 30,
        max_queries_per_run: Number(draft.max_queries_per_run) || 20,
        max_posts_per_query: Number(draft.max_posts_per_query) || 30,
        max_qualified_per_run: Number(draft.max_qualified_per_run) || 100,
      };
      // Edit-mode only: when the user changes the expanded queries
      // textarea, send the new list.  Backend skips re-expansion since
      // we're not changing ``topic``.  On a brand-new search the field
      // is empty and we let the worker generate them after create.
      if (!isNew) {
        const lines = (draft.expanded_queries_text || '')
          .split('\n').map((s) => s.trim()).filter(Boolean);
        payload.expanded_queries = lines;
      }
      return isNew ? createSearch(payload) : updateSearch(search.id, payload);
    },
    onSuccess: (responseData) => {
      // ──────────────────────────────────────────────────────────────
      // Stale-cache bug fix.  ``invalidateQueries`` marks the detail
      // cache stale but doesn't drop the cached body — so on the next
      // open of this modal, the synchronous ``useQuery`` returns the
      // STALE detail.  The hydration block runs once
      // (``if (fullSearch && !hydrated)``), copies stale values into
      // ``draft``, sets ``hydrated=true``, and BLOCKS re-hydration when
      // the background refetch lands.  Net result: the user toggles
      // a checkbox, saves, reopens, and sees the OLD value — looks
      // like the save was ignored even though it persisted to the DB.
      //
      // Fix: seed the detail cache with the PATCH response (which is
      // the new state) BEFORE closing the modal.  Then the next open's
      // hydration copies fresh values.  Belt-and-suspenders: also
      // invalidate so any background usage refetches.
      // ──────────────────────────────────────────────────────────────
      const updatedId = responseData?.id || search?.id;
      if (updatedId && responseData) {
        queryClient.setQueryData(
          ['social-radar-search-detail', updatedId],
          responseData,
        );
      }
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches'] });
      queryClient.invalidateQueries({ queryKey: ['social-radar-searches-min'] });
      if (updatedId) {
        queryClient.invalidateQueries({ queryKey: ['social-radar-search-detail', updatedId] });
        queryClient.invalidateQueries({ queryKey: ['social-radar-estimate', updatedId] });
      }
      toast.success(isNew ? 'Search created' : 'Search saved');
      onClose();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to save'),
  });

  const canSave = draft.name.trim() && draft.topic.trim() && !saveMut.isPending;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl p-6 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
        data-testid="search-editor-modal"
      >
        <div className="flex justify-between items-start mb-4">
          <h2 className="m-0 text-lg font-semibold text-slate-900">
            {isNew ? 'New Social Radar search' : `Edit "${search.name}"`}
          </h2>
          <button type="button" onClick={onClose} aria-label="Close"
            className="text-slate-400 hover:text-slate-600 text-xl bg-transparent border-none cursor-pointer p-1">×</button>
        </div>

        <div className="space-y-4">
          <Field label="Name">
            <input type="text" value={draft.name} onChange={(e) => update('name', e.target.value)}
              data-testid="input-name"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
          </Field>

          <Field label="Topic" help="What you want to find — plain English. e.g. &ldquo;frustrated with our IT provider&rdquo;.">
            <textarea value={draft.topic} onChange={(e) => update('topic', e.target.value)}
              rows={2} data-testid="input-topic"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Niche / audience (optional)">
              <input type="text" value={draft.niche} onChange={(e) => update('niche', e.target.value)}
                placeholder="mid-market east coast"
                data-testid="input-niche"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </Field>
            <Field label="Geography (optional)">
              <input type="text" value={draft.geography} onChange={(e) => update('geography', e.target.value)}
                placeholder="Northeast US"
                data-testid="input-geography"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </Field>
          </div>

          <Field
            label="Cost controls"
            help="Per-run spend cap; the worker tracks Anthropic token usage and aborts before exceeding it.  LinkedIn web-search on Sonnet is the most expensive part of a run and rarely produces results, so it's OFF by default; the watchlist is the reliable LinkedIn channel and always runs when LinkedIn is in your sources."
          >
            <div className="space-y-2">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={!!draft.linkedin_web_search_enabled}
                  onChange={(e) => update('linkedin_web_search_enabled', e.target.checked)}
                  data-testid="input-linkedin-web-search"
                />
                Enable LinkedIn web-search (slow, expensive)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={draft.linkedin_crosslink_enabled !== false}
                  onChange={(e) => update('linkedin_crosslink_enabled', e.target.checked)}
                  data-testid="input-linkedin-crosslink"
                />
                <span>
                  Pull LinkedIn URLs found in Reddit discussions
                  <span className="text-slate-500"> — free, no extra cost</span>
                </span>
              </label>
              <div className="flex items-center gap-2 text-sm">
                <span className="text-slate-600">Max spend per run ($):</span>
                <input
                  type="number" min={0.05} max={100} step={0.25}
                  value={draft.max_run_cost_usd}
                  onChange={(e) => update('max_run_cost_usd', e.target.value)}
                  data-testid="input-max-run-cost"
                  className="w-24 px-3 py-1.5 border border-slate-300 rounded-lg text-sm"
                />
              </div>
            </div>
          </Field>

          <Field
            label="Sources"
            help="Reddit and Twitter/X are heavily indexed and surface most of the buying-intent signal.  LinkedIn alone often returns 0 results because the platform blocks indexing."
          >
            <div className="flex flex-wrap gap-2" data-testid="source-picker">
              {ALL_SOURCES.map((s) => {
                const active = (draft.sources || []).includes(s.value);
                return (
                  <button
                    key={s.value}
                    type="button"
                    role="checkbox"
                    aria-checked={active}
                    data-testid={`source-${s.value}`}
                    onClick={() => {
                      setDraft((d) => {
                        const cur = d.sources || [];
                        const next = active
                          ? cur.filter((x) => x !== s.value)
                          : [...cur, s.value];
                        return { ...d, sources: next };
                      });
                    }}
                    title={s.help}
                    className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
                      active
                        ? 'bg-brand-600 text-white border-brand-600'
                        : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                    }`}
                  >
                    {s.label}
                  </button>
                );
              })}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Include keywords (comma-separated)">
              <input type="text" value={draft.include_keywords} onChange={(e) => update('include_keywords', e.target.value)}
                placeholder="msp, our it provider"
                data-testid="input-include"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </Field>
            <Field label="Exclude keywords (comma-separated)">
              <input type="text" value={draft.exclude_keywords} onChange={(e) => update('exclude_keywords', e.target.value)}
                placeholder="hubspot"
                data-testid="input-exclude"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </Field>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <Field label="Frequency">
              <select value={draft.frequency} onChange={(e) => update('frequency', e.target.value)}
                data-testid="input-frequency"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white">
                {FREQUENCY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </Field>
            <Field label="Status">
              <select value={draft.status} onChange={(e) => update('status', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white">
                <option value="active">Active</option>
                <option value="paused">Paused</option>
                <option value="archived">Archived</option>
              </select>
            </Field>
            <Field label="Tone">
              <input type="text" value={draft.tone} onChange={(e) => update('tone', e.target.value)}
                placeholder="helpful"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </Field>
          </div>

          <Field label="Sender name (used in suggested copy)">
            <input type="text" value={draft.sender_name} onChange={(e) => update('sender_name', e.target.value)}
              data-testid="input-sender-name"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
          </Field>

          <Field
            label="LinkedIn profile watchlist"
            help={`Web search has terrible recall on LinkedIn. Paste profile URLs (one per line, comma-separated, or pasted from a spreadsheet column — any whitespace works) and the worker fetches each profile's recent posts via Unipile every run. Bypasses indexing entirely. Requires a connected LinkedIn account in Settings. Duplicates are deduplicated by slug on save.`}
          >
            <textarea
              value={draft.linkedin_profile_watchlist_text}
              onChange={(e) => update('linkedin_profile_watchlist_text', e.target.value)}
              rows={5}
              data-testid="input-watchlist"
              placeholder={'https://www.linkedin.com/in/jane-doe\nhttps://www.linkedin.com/in/bob-smith\n... (or paste a CSV column)'}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-xs text-slate-900 font-mono focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
            {(() => {
              const { valid, invalid } = classifyWatchlistEntries(
                draft.linkedin_profile_watchlist_text || '',
              );
              if (!valid.length && !invalid.length) return null;
              return (
                <div
                  className="mt-2 flex flex-wrap items-center gap-2 text-xs"
                  data-testid="watchlist-counts"
                >
                  <span className="rounded-full bg-emerald-100 text-emerald-900 px-2 py-0.5">
                    <strong data-testid="watchlist-valid-count">{valid.length}</strong>
                    {' '}valid LinkedIn profile{valid.length === 1 ? '' : 's'}
                  </span>
                  {invalid.length > 0 && (
                    <span
                      className="rounded-full bg-amber-100 text-amber-900 px-2 py-0.5"
                      title={invalid.slice(0, 5).join('\n') + (invalid.length > 5 ? '\n…' : '')}
                    >
                      <strong data-testid="watchlist-invalid-count">{invalid.length}</strong>
                      {' '}invalid (will be dropped on save)
                    </span>
                  )}
                  {valid.length >= 100 && (
                    <span className="text-slate-500">
                      Large watchlists run in parallel — expect ~{Math.ceil(valid.length / 10) * 3}s per run.
                    </span>
                  )}
                </div>
              );
            })()}
          </Field>

          <Field label="Look back (days)" help="Posts older than this are skipped — the LLM is told to ignore them AND we defensively post-filter by post_date.  Default 30; widen to 90 for trend research, tighten to 7 for real-time intent.">
            <input type="number" min={1} max={3650}
              value={draft.max_post_age_days}
              onChange={(e) => update('max_post_age_days', e.target.value)}
              data-testid="input-max-post-age-days"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
          </Field>

          <button type="button" onClick={() => setShowAdvanced((v) => !v)}
            className="text-xs text-slate-500 hover:text-slate-700">
            {showAdvanced ? '▾ Hide' : '▸ Advanced'} cost caps
          </button>
          {showAdvanced && (
            <div className="grid grid-cols-3 gap-3 bg-slate-50 p-3 rounded-lg">
              <Field label="Max queries / run">
                <input type="number" min={1} max={100} value={draft.max_queries_per_run}
                  onChange={(e) => update('max_queries_per_run', e.target.value)}
                  className="w-full px-3 py-1.5 border border-slate-300 rounded-lg text-sm" />
              </Field>
              <Field label="Max posts / query">
                <input type="number" min={1} max={100} value={draft.max_posts_per_query}
                  onChange={(e) => update('max_posts_per_query', e.target.value)}
                  className="w-full px-3 py-1.5 border border-slate-300 rounded-lg text-sm" />
              </Field>
              <Field label="Max qualified / run">
                <input type="number" min={1} max={500} value={draft.max_qualified_per_run}
                  onChange={(e) => update('max_qualified_per_run', e.target.value)}
                  className="w-full px-3 py-1.5 border border-slate-300 rounded-lg text-sm" />
              </Field>
            </div>
          )}

          {/* Editable expanded queries — only on edit. */}
          {!isNew && (
            <div className="border-t border-slate-200 pt-3">
              <Field
                label="Search phrases (one per line)"
                help={`These are the LinkedIn search phrases the AI generated from your topic.  Edit, add, or remove freely — short single-word entries are ignored (they surface SEO content instead of real posts).  Save updates the list immediately and the next run uses it.  Changing "topic" above will overwrite this list.`}
              >
                <textarea
                  value={draft.expanded_queries_text}
                  onChange={(e) => update('expanded_queries_text', e.target.value)}
                  rows={8}
                  data-testid="input-expanded-queries"
                  placeholder={'frustrated with our msp\nanyone else fed up with their phone vendor\nlooking for a new internet provider for our DC office'}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-xs text-slate-900 font-mono focus:outline-none focus:ring-2 focus:ring-brand-500"
                />
              </Field>
            </div>
          )}

          {/* Preview-queries */}
          <div className="border-t border-slate-200 pt-3">
            <div className="flex justify-between items-center mb-2">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Preview expanded queries
              </span>
              <button type="button"
                onClick={() => previewMut.mutate()}
                disabled={!draft.topic.trim() || previewMut.isPending}
                data-testid="preview-queries-btn"
                className="px-3 py-1 text-xs bg-slate-100 hover:bg-slate-200 rounded-lg disabled:opacity-50">
                {previewMut.isPending ? 'Expanding…' : 'Run preview'}
              </button>
            </div>
            {preview && (
              <div className="flex flex-wrap gap-1.5" data-testid="preview-chips">
                {preview.length === 0
                  ? <span className="text-xs text-slate-400 italic">No queries returned.</span>
                  : preview.map((q) => (
                    <span key={q} className="px-2 py-0.5 bg-brand-50 text-brand-700 text-xs rounded">{q}</span>
                  ))
                }
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 pt-3 border-t border-slate-200">
            <button type="button" onClick={onClose}
              className="px-3 py-1.5 text-sm border border-slate-300 rounded-lg text-slate-700 hover:bg-slate-50">
              Cancel
            </button>
            <button type="button"
              onClick={() => saveMut.mutate()}
              disabled={!canSave}
              data-testid="save-search-btn"
              className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
              {saveMut.isPending ? 'Saving…' : (isNew ? 'Create search' : 'Save changes')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


function Field({ label, help, children }) {
  return (
    <label className="block">
      <span className="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">{label}</span>
      {children}
      {help && <span className="block text-xs text-slate-500 mt-1">{help}</span>}
    </label>
  );
}
