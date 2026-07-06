import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getIntentStatus,
  listIntentProfiles,
  createIntentPreset,
  updateIntentProfile,
  activateIntentProfile,
  deleteIntentProfile,
  getRankedIntent,
  seedIntentOrgs,
  runIntentCollectors,
  recomputeIntent,
  promoteIntent,
} from '../api/intent.js';
import { useToast } from '../components/Toast.jsx';
import { Button } from '../components/ui.jsx';

const TIER_BADGE = {
  1: 'bg-red-100 text-red-700',
  2: 'bg-amber-100 text-amber-700',
  3: 'bg-slate-100 text-slate-600',
};
const TIER_LABEL = { 1: 'Tier 1 · act now', 2: 'Tier 2 · warm', 3: 'Tier 3 · list' };

function Stat({ label, value, testId }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm px-4 py-3" data-testid={testId}>
      <div className="text-2xl font-bold text-slate-900 tabular-nums">{value}</div>
      <div className="text-xs text-slate-500 mt-0.5">{label}</div>
    </div>
  );
}

// --------------------------------------------------------------------------
// ICP profile editor
// --------------------------------------------------------------------------

function ProfileEditor({ profile }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState(null);

  useEffect(() => {
    setForm({
      name: profile.name,
      cause_codes: (profile.cause_codes || []).join(', '),
      geographies: (profile.geographies || []).join(', '),
      rfp_keywords: (profile.rfp_keywords || []).join('\n'),
      promotion_threshold: String(profile.promotion_threshold ?? 80),
      size_band_weights: JSON.stringify(profile.size_band_weights || {}, null, 2),
      signal_weights: JSON.stringify(profile.signal_weights || {}, null, 2),
      half_life_overrides: JSON.stringify(profile.half_life_overrides || {}, null, 2),
    });
  }, [profile.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveMut = useMutation({
    mutationFn: () => {
      let sizeW, sigW, hlW;
      try {
        sizeW = JSON.parse(form.size_band_weights || '{}');
        sigW = JSON.parse(form.signal_weights || '{}');
        hlW = JSON.parse(form.half_life_overrides || '{}');
      } catch {
        throw new Error('Weights / overrides must be valid JSON');
      }
      return updateIntentProfile(profile.id, {
        name: form.name,
        cause_codes: form.cause_codes.split(',').map((s) => s.trim()).filter(Boolean),
        geographies: form.geographies.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean),
        rfp_keywords: form.rfp_keywords.split('\n').map((s) => s.trim()).filter(Boolean),
        promotion_threshold: Number(form.promotion_threshold),
        size_band_weights: sizeW,
        signal_weights: sigW,
        half_life_overrides: hlW,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['intent-profiles'] });
      queryClient.invalidateQueries({ queryKey: ['intent-feed'] });
      toast.success('Profile saved');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || err.message || 'Save failed'),
  });

  if (!form) return null;
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const labelCls = 'block text-xs font-medium text-slate-600 mb-1';
  const inputCls = 'w-full text-sm border border-slate-300 rounded-lg px-3 py-2 focus-visible:ring-2 focus-visible:ring-brand-500 outline-none';

  return (
    <div className="space-y-3" data-testid="intent-profile-editor">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>Profile name</label>
          <input className={inputCls} value={form.name} onChange={set('name')} data-testid="profile-name" />
        </div>
        <div>
          <label className={labelCls}>Promotion threshold</label>
          <input className={inputCls} type="number" value={form.promotion_threshold}
            onChange={set('promotion_threshold')} data-testid="profile-threshold" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>Cause codes (NTEE prefixes, comma-sep)</label>
          <input className={inputCls} value={form.cause_codes} onChange={set('cause_codes')} placeholder="A, B, E, T" />
        </div>
        <div>
          <label className={labelCls}>Geographies (states, comma-sep — blank = any)</label>
          <input className={inputCls} value={form.geographies} onChange={set('geographies')} placeholder="CA, NY" />
        </div>
      </div>
      <div>
        <label className={labelCls}>RFP keywords (one per line — Grants.gov search)</label>
        <textarea className={inputCls} rows={3} value={form.rfp_keywords} onChange={set('rfp_keywords')} />
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-slate-600 font-medium">Advanced weights (JSON)</summary>
        <div className="grid grid-cols-3 gap-3 mt-2">
          <div>
            <label className={labelCls}>Size-band weights</label>
            <textarea className={`${inputCls} font-mono text-xs`} rows={6}
              value={form.size_band_weights} onChange={set('size_band_weights')} />
          </div>
          <div>
            <label className={labelCls}>Signal weights</label>
            <textarea className={`${inputCls} font-mono text-xs`} rows={6}
              value={form.signal_weights} onChange={set('signal_weights')} />
          </div>
          <div>
            <label className={labelCls}>Half-life overrides (days)</label>
            <textarea className={`${inputCls} font-mono text-xs`} rows={6}
              value={form.half_life_overrides} onChange={set('half_life_overrides')} />
          </div>
        </div>
      </details>
      <Button onClick={() => saveMut.mutate()} loading={saveMut.isPending} data-testid="profile-save">
        Save profile
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------------
// Ranked intent feed
// --------------------------------------------------------------------------

function IntentFeed({ profileId }) {
  const { data = [], isLoading, error } = useQuery({
    queryKey: ['intent-feed', profileId],
    queryFn: () => getRankedIntent(profileId, 50),
    enabled: !!profileId,
  });
  if (isLoading) return <p className="text-sm text-slate-500">Loading intent feed…</p>;
  if (error) return <p className="text-sm text-red-600">Failed to load intent feed</p>;
  if (!data.length) {
    return (
      <p className="text-sm text-slate-500" data-testid="intent-feed-empty">
        No scored orgs yet. Seed some orgs, run the collectors, then recompute.
      </p>
    );
  }
  return (
    <table className="w-full text-sm" data-testid="intent-feed">
      <thead>
        <tr className="text-left text-xs text-slate-500 border-b border-slate-200">
          <th className="py-2 pr-3">Tier</th>
          <th className="py-2 pr-3">Score</th>
          <th className="py-2 pr-3">Organization</th>
          <th className="py-2 pr-3">Why now</th>
        </tr>
      </thead>
      <tbody>
        {data.map((r) => (
          <tr key={r.org_id} className="border-b border-slate-100 align-top" data-testid="intent-feed-row">
            <td className="py-2 pr-3">
              <span className={`inline-block rounded-pill px-2 py-0.5 text-xs font-medium ${TIER_BADGE[r.tier] || ''}`}>
                {TIER_LABEL[r.tier] || `Tier ${r.tier}`}
              </span>
            </td>
            <td className="py-2 pr-3 tabular-nums font-medium text-slate-900">{r.intent_score}</td>
            <td className="py-2 pr-3">
              <div className="font-medium text-slate-900">{r.name}</div>
              <div className="text-xs text-slate-400">
                {[r.size_band, r.state, r.ntee_code].filter(Boolean).join(' · ')}
              </div>
            </td>
            <td className="py-2 pr-3 text-slate-600">
              {r.why_now || '—'}
              {r.evidence_url && (
                <>
                  {' '}
                  <a href={r.evidence_url} target="_blank" rel="noreferrer"
                    className="text-brand-600 hover:underline whitespace-nowrap">evidence ↗</a>
                </>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// --------------------------------------------------------------------------
// The tab
// --------------------------------------------------------------------------

export default function IntentTab() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [seedQuery, setSeedQuery] = useState('community foundation');
  const [seedLimit, setSeedLimit] = useState(50);

  const { data: status } = useQuery({ queryKey: ['intent-status'], queryFn: getIntentStatus });
  const { data: profiles = [] } = useQuery({ queryKey: ['intent-profiles'], queryFn: listIntentProfiles });

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ['intent-status'] });
    queryClient.invalidateQueries({ queryKey: ['intent-profiles'] });
    queryClient.invalidateQueries({ queryKey: ['intent-feed'] });
  };

  const presetMut = useMutation({
    mutationFn: (kind) => createIntentPreset(kind),
    onSuccess: () => { invalidateAll(); toast.success('Profile created + activated'); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const activateMut = useMutation({
    mutationFn: (id) => activateIntentProfile(id),
    onSuccess: () => { invalidateAll(); toast.success('Profile activated'); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const deleteMut = useMutation({
    mutationFn: (id) => deleteIntentProfile(id),
    onSuccess: () => { invalidateAll(); toast.success('Profile deleted'); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const seedMut = useMutation({
    mutationFn: () => seedIntentOrgs({ query: seedQuery, limit: Number(seedLimit) }),
    onSuccess: (d) => { invalidateAll(); toast.success(`Seeded ${d.created} new orgs (${d.orgs_total} total)`); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Seed failed'),
  });
  const collectMut = useMutation({
    mutationFn: runIntentCollectors,
    onSuccess: () => toast.success('Collectors queued — recompute in ~1 min for results'),
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const recomputeMut = useMutation({
    mutationFn: recomputeIntent,
    onSuccess: (d) => { invalidateAll(); toast.success(`Recomputed ${d.orgs} orgs (T1 ${d.tier1} · T2 ${d.tier2} · T3 ${d.tier3})`); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });
  const promoteMut = useMutation({
    mutationFn: promoteIntent,
    onSuccess: (d) => { invalidateAll(); toast.success(`Promoted ${d.promoted} org(s) → draft campaign`); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed'),
  });

  const activeId = status?.active_profile_id;
  const activeProfile = profiles.find((p) => p.id === activeId);
  const cardCls = 'bg-white rounded-xl border border-slate-200 shadow-sm p-5 mb-4';

  return (
    <div data-testid="intent-tab">
      <h2 className="text-lg font-semibold text-slate-900 mb-1">Intent engine</h2>
      <p className="text-sm text-slate-500 mt-0 mb-5">
        Surfaces nonprofits with a <strong>reason to act now</strong> — a posted dev/grant role, a new
        matching RFP, a peer's federal award, a 990 revenue drop — scored + tiered against an ICP profile.
        Orgs over the promotion threshold become <strong>approval-pending drafts</strong> under Campaigns;
        nothing ever sends or converts without your click.
      </p>

      {status && !status.adzuna_configured && (
        <div data-testid="adzuna-warning"
          className="mb-4 bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-lg px-4 py-3">
          No Adzuna key configured — the job-posting (dev-role) collector is off. Add
          {' '}<code>ADZUNA_APP_ID</code> / <code>ADZUNA_APP_KEY</code> and recreate the containers to enable it.
          The careers-page, ATS, and funding collectors work without it.
        </div>
      )}

      {/* Dashboard */}
      <div className="grid grid-cols-5 gap-3 mb-4">
        <Stat label="Monitored orgs" value={status?.orgs_total ?? '—'} testId="stat-orgs" />
        <Stat label="Tier 1 (act now)" value={status?.tiers?.['1'] ?? 0} />
        <Stat label="Tier 2 (warm)" value={status?.tiers?.['2'] ?? 0} />
        <Stat label="Tier 3 (list)" value={status?.tiers?.['3'] ?? 0} />
        <Stat label="Pending drafts" value={status?.draft_pending_leads ?? 0} testId="stat-drafts" />
      </div>
      {status?.draft_campaign_id && status?.draft_pending_leads > 0 && (
        <p className="text-sm text-slate-600 -mt-2 mb-4">
          {status.draft_pending_leads} approval-pending draft(s) waiting in{' '}
          <a href={`/campaigns/${status.draft_campaign_id}`} className="text-brand-600 hover:underline">
            the intent-drafts campaign
          </a>.
        </p>
      )}

      {/* Pipeline actions */}
      <div className={cardCls}>
        <h3 className="text-base font-semibold text-slate-900 m-0 mb-3">Run the pipeline</h3>
        <div className="flex flex-wrap items-end gap-2 mb-3">
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Seed orgs by cause keyword</label>
            <input data-testid="seed-query" value={seedQuery} onChange={(e) => setSeedQuery(e.target.value)}
              className="text-sm border border-slate-300 rounded-lg px-3 py-2 w-64 outline-none focus-visible:ring-2 focus-visible:ring-brand-500" />
          </div>
          <input data-testid="seed-limit" type="number" value={seedLimit} onChange={(e) => setSeedLimit(e.target.value)}
            className="text-sm border border-slate-300 rounded-lg px-3 py-2 w-20 outline-none focus-visible:ring-2 focus-visible:ring-brand-500" />
          <Button variant="secondary" onClick={() => seedMut.mutate()} loading={seedMut.isPending} data-testid="seed-btn">
            Seed orgs
          </Button>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => collectMut.mutate()} loading={collectMut.isPending} data-testid="run-collectors-btn">
            Run collectors
          </Button>
          <Button variant="secondary" onClick={() => recomputeMut.mutate()} loading={recomputeMut.isPending} data-testid="recompute-btn">
            Recompute scores
          </Button>
          <Button onClick={() => promoteMut.mutate()} loading={promoteMut.isPending} data-testid="promote-btn">
            Promote eligible → drafts
          </Button>
        </div>
        <p className="text-xs text-slate-400 mt-2">
          Collectors hit external APIs (async). Recompute + promote run immediately. These also run daily on a schedule.
        </p>
      </div>

      {/* ICP profile */}
      <div className={cardCls}>
        <h3 className="text-base font-semibold text-slate-900 m-0 mb-3">ICP profile</h3>
        {profiles.length === 0 ? (
          <div className="flex gap-2" data-testid="intent-no-profile">
            <Button onClick={() => presetMut.mutate('grantmind')} loading={presetMut.isPending} data-testid="preset-grantmind">
              Create GrantMind profile
            </Button>
            <Button variant="secondary" onClick={() => presetMut.mutate('generic')} loading={presetMut.isPending}>
              Create generic profile
            </Button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-2 mb-4">
              {profiles.map((p) => (
                <div key={p.id}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm ${
                    p.id === activeId ? 'border-brand-500 bg-brand-50' : 'border-slate-200'}`}>
                  <span className="font-medium text-slate-800">{p.name}</span>
                  {p.id === activeId
                    ? <span className="text-xs text-brand-600 font-medium">active</span>
                    : <button className="text-xs text-brand-600 hover:underline"
                        onClick={() => activateMut.mutate(p.id)} data-testid={`activate-${p.id}`}>activate</button>}
                  <button className="text-xs text-slate-400 hover:text-red-600"
                    onClick={() => { if (confirm(`Delete profile "${p.name}"?`)) deleteMut.mutate(p.id); }}>✕</button>
                </div>
              ))}
              <Button size="sm" variant="ghost" onClick={() => presetMut.mutate('grantmind')}>+ GrantMind preset</Button>
            </div>
            {activeProfile && <ProfileEditor key={activeProfile.id} profile={activeProfile} />}
          </>
        )}
      </div>

      {/* Ranked feed */}
      <div className={cardCls}>
        <h3 className="text-base font-semibold text-slate-900 m-0 mb-3">Ranked intent</h3>
        {activeId
          ? <IntentFeed profileId={activeId} />
          : <p className="text-sm text-slate-500">Create + activate a profile to see the ranked intent list.</p>}
      </div>
    </div>
  );
}
