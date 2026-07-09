import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listTenants,
  overridePlan,
  impersonateTenant,
  grantFreeAccess,
  cancelAccess,
  deleteTenant,
  getStats,
  listInviteLinks,
  createInviteLink,
  revokeInviteLink,
  listAudit,
} from '../api/admin.js';

const PLANS = ['starter', 'pro', 'agency'];

function StatCard({ label, value, sub }) {
  return (
    <div className="bg-white border border-slate-200 rounded-lg p-4 shadow-sm">
      <p className="text-xs font-medium text-slate-500 uppercase tracking-wider">{label}</p>
      <p className="text-2xl font-bold text-slate-900 mt-1">{value}</p>
      {sub && <p className="text-xs text-slate-400 mt-1 truncate">{sub}</p>}
    </div>
  );
}

function StatsPanel() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ['admin-stats'],
    queryFn: getStats,
  });

  if (isLoading) return <p className="text-slate-400 text-sm">Loading stats...</p>;
  if (!stats) return null;

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard label="Total Workspaces" value={stats.total_tenants} />
      <StatCard label="Total Users" value={stats.total_users} />
      <StatCard label="Emails Sent (All Time)" value={stats.total_emails_sent.toLocaleString()} />
      <StatCard
        label="Last Signup"
        value={stats.last_signup_at ? new Date(stats.last_signup_at).toLocaleDateString() : '—'}
        sub={stats.last_signup_email}
      />
    </div>
  );
}

function InviteLinkCreator() {
  const queryClient = useQueryClient();
  const { data: links, isLoading } = useQuery({
    queryKey: ['admin-invite-links'],
    queryFn: listInviteLinks,
  });
  const [label, setLabel] = useState('');
  const [trialDays, setTrialDays] = useState(14);
  const [maxUses, setMaxUses] = useState('');
  const [creating, setCreating] = useState(false);
  const [copied, setCopied] = useState(null);
  const [justCreated, setJustCreated] = useState(null);

  async function handleCreate(e) {
    e.preventDefault();
    setCreating(true);
    try {
      const newLink = await createInviteLink({
        label: label || null,
        trial_days: trialDays,
        max_uses: maxUses ? parseInt(maxUses, 10) : null,
      });
      queryClient.invalidateQueries({ queryKey: ['admin-invite-links'] });
      setJustCreated(newLink);
      setLabel('');
      setTrialDays(14);
      setMaxUses('');
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(linkId) {
    await revokeInviteLink(linkId);
    queryClient.invalidateQueries({ queryKey: ['admin-invite-links'] });
    if (justCreated?.id === linkId) setJustCreated(null);
  }

  function copyUrl(url) {
    navigator.clipboard.writeText(url);
    setCopied(url);
    setTimeout(() => setCopied(null), 2000);
  }

  const activeLinks = (links || []).filter(l => !l.revoked && (!l.max_uses || l.use_count < l.max_uses));

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm">
      <div className="p-5 border-b border-slate-200">
        <h2 className="text-lg font-semibold text-slate-900">Signup Links</h2>
        <p className="text-sm text-slate-500 mt-1">
          Generate invite links to let beta testers sign up without a credit card. You can set custom trial durations and usage limits.
        </p>
      </div>

      <form onSubmit={handleCreate} className="p-5 border-b border-slate-100 bg-slate-50">
        <div className="flex flex-wrap gap-3 items-end">
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Label (optional)</span>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Beta batch 1"
              className="mt-1 block w-48 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Trial days</span>
            <input
              type="number"
              min={1}
              max={365}
              value={trialDays}
              onChange={(e) => setTrialDays(parseInt(e.target.value, 10) || 14)}
              className="mt-1 block w-20 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Max uses</span>
            <input
              type="number"
              min={1}
              value={maxUses}
              onChange={(e) => setMaxUses(e.target.value)}
              placeholder="Unlimited"
              className="mt-1 block w-28 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
            />
          </label>
          <button
            type="submit"
            disabled={creating}
            className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-1.5 rounded-md transition-colors"
          >
            {creating ? 'Creating...' : 'Generate Link'}
          </button>
        </div>
      </form>

      {justCreated && (
        <div className="p-4 mx-5 mt-4 bg-green-50 border border-green-200 rounded-lg">
          <p className="text-sm font-medium text-green-800 mb-2">Link created! Send this to your beta tester:</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs bg-white border border-green-300 rounded px-3 py-2 text-green-900 break-all select-all">
              {justCreated.url}
            </code>
            <button
              onClick={() => copyUrl(justCreated.url)}
              className="shrink-0 bg-green-600 hover:bg-green-700 text-white text-xs font-medium px-3 py-2 rounded-md transition-colors"
            >
              {copied === justCreated.url ? 'Copied!' : 'Copy'}
            </button>
          </div>
        </div>
      )}

      <div className="p-5">
        {isLoading ? (
          <p className="text-slate-400 text-sm">Loading links...</p>
        ) : !links?.length ? (
          <p className="text-slate-400 text-sm">No invite links created yet. Use the form above to generate one.</p>
        ) : (
          <>
            <p className="text-xs font-medium text-slate-500 uppercase tracking-wider mb-3">
              All Links ({activeLinks.length} active)
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-3 py-2">Label</th>
                    <th className="px-3 py-2">Trial</th>
                    <th className="px-3 py-2">Uses</th>
                    <th className="px-3 py-2">Status</th>
                    <th className="px-3 py-2">Created</th>
                    <th className="px-3 py-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {links.map((link) => (
                    <tr key={link.id} className="border-b border-slate-100 hover:bg-slate-50">
                      <td className="px-3 py-2 font-medium text-slate-900">{link.label || '—'}</td>
                      <td className="px-3 py-2 text-slate-600">{link.trial_days} days</td>
                      <td className="px-3 py-2 text-slate-600 tabular-nums">
                        {link.use_count}{link.max_uses ? ` / ${link.max_uses}` : ''}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`inline-block text-xs px-2 py-0.5 rounded-full font-medium ${
                          link.revoked ? 'bg-red-100 text-red-700' :
                          (link.max_uses && link.use_count >= link.max_uses)
                            ? 'bg-slate-100 text-slate-500' : 'bg-green-100 text-green-700'
                        }`}>
                          {link.revoked ? 'Revoked' :
                           (link.max_uses && link.use_count >= link.max_uses) ? 'Exhausted' : 'Active'}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-500 text-xs whitespace-nowrap">
                        {new Date(link.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => copyUrl(link.url)}
                            className="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
                          >
                            {copied === link.url ? 'Copied!' : 'Copy'}
                          </button>
                          {!link.revoked && (
                            <button
                              onClick={() => handleRevoke(link.id)}
                              className="text-xs text-red-500 hover:text-red-700 font-medium"
                            >
                              Revoke
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function TenantsTable() {
  const queryClient = useQueryClient();
  const { data: tenants, isLoading, isError } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: listTenants,
  });
  const [busy, setBusy] = useState(null);
  const [confirm, setConfirm] = useState(null);

  async function handlePlanChange(tenantId, plan) {
    setBusy(tenantId);
    try {
      await overridePlan(tenantId, { plan, subscription_status: 'active' });
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    } finally {
      setBusy(null);
    }
  }

  async function handleImpersonate(tenantId) {
    setBusy(tenantId);
    try {
      await impersonateTenant(tenantId);
      queryClient.invalidateQueries({ queryKey: ['auth-me'] });
      window.location.assign('/campaigns');
    } finally {
      setBusy(null);
    }
  }

  async function handleGrantFree(tenantId) {
    setBusy(tenantId);
    try {
      await grantFreeAccess(tenantId);
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    } finally {
      setBusy(null);
    }
  }

  async function handleCancel(tenantId) {
    setBusy(tenantId);
    try {
      await cancelAccess(tenantId);
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    } finally {
      setBusy(null);
    }
  }

  async function handleDelete(tenantId) {
    setBusy(tenantId);
    try {
      await deleteTenant(tenantId);
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
      queryClient.invalidateQueries({ queryKey: ['admin-stats'] });
      setConfirm(null);
    } finally {
      setBusy(null);
    }
  }

  if (isLoading) return <p className="text-slate-400 text-sm p-4">Loading tenants...</p>;
  if (isError) return <p className="text-red-500 text-sm p-4">Failed to load tenants.</p>;

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm">
      <div className="p-5 border-b border-slate-200">
        <h2 className="text-lg font-semibold text-slate-900">Tenants</h2>
        <p className="text-sm text-slate-500 mt-1">Manage workspaces, plans, and access.</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="text-xs uppercase text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-4 py-3">Owner</th>
              <th className="px-4 py-3">Workspace</th>
              <th className="px-4 py-3">Plan</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Emails</th>
              <th className="px-4 py-3">Joined</th>
              <th className="px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody>
            {tenants.map((t) => (
              <tr key={t.id} className="border-b border-slate-100 hover:bg-slate-50">
                <td className="px-4 py-3 text-xs text-slate-600">{t.owner_email || '—'}</td>
                <td className="px-4 py-3">
                  <span className="font-medium text-slate-900">{t.name}</span>
                  <span className="text-slate-400 text-xs ml-1">({t.members})</span>
                </td>
                <td className="px-4 py-3">
                  <select
                    value={t.plan || ''}
                    disabled={busy === t.id}
                    onChange={(e) => handlePlanChange(t.id, e.target.value || null)}
                    className="text-xs border border-slate-300 rounded px-2 py-1 bg-white"
                  >
                    <option value="">none</option>
                    {PLANS.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-3">
                  <span className={`inline-block text-xs px-2 py-0.5 rounded-full font-medium ${
                    t.subscription_status === 'active' ? 'bg-green-100 text-green-700' :
                    t.subscription_status === 'trialing' ? 'bg-blue-100 text-blue-700' :
                    t.subscription_status === 'past_due' ? 'bg-yellow-100 text-yellow-700' :
                    t.subscription_status === 'canceled' ? 'bg-red-100 text-red-700' :
                    'bg-slate-100 text-slate-500'
                  }`}>
                    {t.subscription_status || 'none'}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-600 text-xs tabular-nums">{t.email_sends.toLocaleString()}</td>
                <td className="px-4 py-3 text-slate-500 text-xs whitespace-nowrap">
                  {t.created_at ? new Date(t.created_at).toLocaleDateString() : '—'}
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => handleImpersonate(t.id)}
                      disabled={busy === t.id}
                      className="text-xs text-indigo-600 hover:text-indigo-800 font-medium disabled:opacity-50"
                    >
                      Impersonate
                    </button>
                    {t.subscription_status !== 'active' && (
                      <button
                        onClick={() => handleGrantFree(t.id)}
                        disabled={busy === t.id}
                        className="text-xs text-green-600 hover:text-green-800 font-medium disabled:opacity-50"
                      >
                        Grant Free
                      </button>
                    )}
                    {t.subscription_status === 'active' && (
                      <button
                        onClick={() => handleCancel(t.id)}
                        disabled={busy === t.id}
                        className="text-xs text-yellow-600 hover:text-yellow-800 font-medium disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    )}
                    {confirm === t.id ? (
                      <span className="flex items-center gap-1">
                        <button
                          onClick={() => handleDelete(t.id)}
                          disabled={busy === t.id}
                          className="text-xs text-red-600 hover:text-red-800 font-bold disabled:opacity-50"
                        >
                          Yes, delete
                        </button>
                        <button
                          onClick={() => setConfirm(null)}
                          className="text-xs text-slate-400 hover:text-slate-600"
                        >
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button
                        onClick={() => setConfirm(t.id)}
                        disabled={busy === t.id}
                        className="text-xs text-red-500 hover:text-red-700 font-medium disabled:opacity-50"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AuditLog() {
  const { data: audit, isLoading, isError } = useQuery({
    queryKey: ['admin-audit'],
    queryFn: () => listAudit(50),
  });

  if (isLoading) return <p className="text-slate-400 text-sm p-4">Loading audit log...</p>;
  if (isError) return <p className="text-red-500 text-sm p-4">Failed to load audit log.</p>;
  if (!audit.length) return <p className="text-slate-400 text-sm p-4">No audit entries yet.</p>;

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm">
      <div className="p-5 border-b border-slate-200">
        <h2 className="text-lg font-semibold text-slate-900">Audit Log</h2>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="text-xs uppercase text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-4 py-3">Time</th>
              <th className="px-4 py-3">Actor</th>
              <th className="px-4 py-3">Action</th>
              <th className="px-4 py-3">Detail</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((entry, i) => (
              <tr key={i} className="border-b border-slate-100 hover:bg-slate-50">
                <td className="px-4 py-3 text-slate-500 text-xs whitespace-nowrap">
                  {new Date(entry.at).toLocaleString()}
                </td>
                <td className="px-4 py-3 font-mono text-xs">{entry.actor}</td>
                <td className="px-4 py-3">
                  <span className="text-xs bg-slate-100 text-slate-700 px-2 py-0.5 rounded font-medium">
                    {entry.action}
                  </span>
                </td>
                <td className="px-4 py-3 text-xs text-slate-500 max-w-xs truncate">
                  {entry.detail ? JSON.stringify(entry.detail) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const TABS = [
  ['overview', 'Overview'],
  ['tenants', 'Tenants'],
  ['audit', 'Audit Log'],
];

export default function Admin() {
  const [tab, setTab] = useState('overview');

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-900">Admin</h1>
        <p className="text-sm text-slate-500 mt-1">Super-admin panel — manage tenants, invite links, and monitor platform activity</p>
      </div>

      <div className="flex gap-1 mb-6 border-b border-slate-200">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              tab === key
                ? 'border-indigo-600 text-indigo-600'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="space-y-6">
          <StatsPanel />
          <InviteLinkCreator />
          <TenantsTable />
        </div>
      )}
      {tab === 'tenants' && <TenantsTable />}
      {tab === 'audit' && <AuditLog />}
    </div>
  );
}
