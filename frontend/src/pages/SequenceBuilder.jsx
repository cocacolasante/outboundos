import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import {
  getSequence,
  getSequenceAnalytics,
  updateSequence,
  validateSequence,
  publishSequence,
} from '../api/sequences.js';

// Kinds the publish step currently accepts.  The backend's full
// SequenceNodeKind enum is wider (it still has linkedin_invite_to_page
// + linkedin_inmail for historical rows / future re-enable), but the
// palette only shows what's actually runnable today — anything else
// just produces a publish error.
//   - linkedin_invite_to_page: Unipile's passthrough whitelist blocks
//     the Voyager endpoint we need.
//   - linkedin_inmail: blocked until Sales Nav API access is enabled
//     on the Unipile workspace.
// Both are tracked in CLAUDE.md "Next up".
const PALETTE = [
  { kind: 'email', label: 'Email', hint: 'Send a templated email.' },
  { kind: 'email_reply', label: 'Reply', hint: 'Reply in-thread to the lead’s original campaign email (not a new thread). AI-written or manual.' },
  { kind: 'wait', label: 'Wait', hint: 'Pause N minutes / hours / days before the next step.' },
  { kind: 'linkedin_view_profile', label: 'LI: View profile', hint: 'Ghost-view the lead’s profile (low-touch warm-up).' },
  { kind: 'linkedin_follow_profile', label: 'LI: Follow profile', hint: 'Follow the lead. They get a notification.' },
  { kind: 'linkedin_react_post', label: 'LI: React to post', hint: 'Like the lead’s most recent post.' },
  { kind: 'linkedin_connect', label: 'LI: Connect', hint: 'Send a connection request, optionally with a 200-char note. The next edge defaults to "if accepted" so a downstream DM waits for the prospect to accept.' },
  { kind: 'linkedin_dm', label: 'LI: DM', hint: 'Send a direct message. Only fires for accepted (1st-degree) connections.' },
  { kind: 'linkedin_comment_post', label: 'LI: Comment on post', hint: 'Comment on the lead’s post — publicly visible. Use sparingly.' },
];

// Build the dnd payload the user drops onto the canvas.
function dragKind(e, kind) {
  e.dataTransfer.setData('application/sequence-kind', kind);
  e.dataTransfer.effectAllowed = 'move';
}

// Shallow equality for node stats — only the fields NodeCard renders,
// keyed exactly the way the analytics endpoint returns them. Returning the
// same node reference when stats are unchanged avoids triggering xyflow's
// re-measure cycle.
function statsEqual(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.attempted === b.attempted
    && a.sent === b.sent
    && a.skipped === b.skipped
    && a.failed === b.failed
    && a.currently_here === b.currently_here
  );
}


// ---- Wait-node duration helpers ----
// Storage stays as `config.duration_minutes` (a single integer) so the backend
// is untouched; the UI just lets the user think in minutes / hours / days /
// weeks instead of doing mental math.

const WAIT_UNITS = [
  { key: 'minutes', label: 'minutes', minutes: 1 },
  { key: 'hours',   label: 'hours',   minutes: 60 },
  { key: 'days',    label: 'days',    minutes: 60 * 24 },
  { key: 'weeks',   label: 'weeks',   minutes: 60 * 24 * 7 },
];

const WAIT_PRESETS = [
  { label: 'Immediate', minutes: 0 },
  { label: '1 hour',    minutes: 60 },
  { label: '1 day',     minutes: 60 * 24 },
  { label: '3 days',    minutes: 60 * 24 * 3 },
  { label: '1 week',    minutes: 60 * 24 * 7 },
];

// Pick the largest unit that divides the duration cleanly so the input shows
// "3 days" instead of "4320 minutes" when re-opened.  Falls back to minutes
// when the duration doesn't evenly divide.
function splitDuration(minutes) {
  const m = Number.isFinite(minutes) ? Math.max(0, Math.floor(minutes)) : 0;
  for (const u of [...WAIT_UNITS].reverse()) {
    if (m >= u.minutes && m % u.minutes === 0) {
      return { value: m / u.minutes, unit: u.key };
    }
  }
  return { value: m, unit: 'minutes' };
}

function unitMinutes(unitKey) {
  return (WAIT_UNITS.find((u) => u.key === unitKey) || WAIT_UNITS[0]).minutes;
}

function formatWaitLabel(minutes) {
  if (minutes == null || Number.isNaN(minutes)) return 'Wait';
  if (minutes <= 0) return 'Immediate';
  const { value, unit } = splitDuration(minutes);
  const label = value === 1 ? unit.replace(/s$/, '') : unit;
  return `Wait ${value} ${label}`;
}


// Sensible per-kind defaults applied when the user first drops a node.
function defaultsForKind(kind) {
  switch (kind) {
    case 'wait':
      return { config: { duration_minutes: 4320 }, title: 'Wait 3 days' };
    case 'linkedin_view_profile':
      return { config: {}, title: 'View profile' };
    case 'linkedin_follow_profile':
      return { config: {}, title: 'Follow profile' };
    case 'linkedin_react_post':
      return { config: { reaction: 'LIKE' }, title: 'React to latest post' };
    case 'linkedin_connect':
      return {
        config: {
          no_note: false,
          note_template: 'Hi {{first_name}}, would love to connect.',
        },
        title: 'Connect request',
      };
    case 'linkedin_dm':
      return {
        config: { ai_compose: true, text_template: '' },
        title: 'Send AI DM',
      };
    case 'linkedin_invite_to_page':
      return { config: { page_id: '' }, title: 'Invite to page' };
    case 'linkedin_inmail':
      return {
        config: {
          subject_template: 'Quick question, {{first_name}}',
          body_template: 'Hi {{first_name}}, I work with {{company}}-style teams and wanted to reach out about...',
        },
        title: 'InMail',
      };
    case 'linkedin_comment_post':
      return {
        config: {
          comment_template: 'Great point, {{first_name}} — really resonated.',
          target: 'latest',
        },
        title: 'Comment on post',
      };
    case 'email_reply':
      return {
        config: { ai_compose: true, ai_prompt: '', body_template: '' },
        title: 'Reply in thread',
      };
    case 'email':
    default:
      return { config: {}, title: 'New email' };
  }
}


// Short human label for an edge's condition, shown right on the arrow.
// Keep this terse — the full JSON is in the right-side editor.
function edgeLabel(condition) {
  if (!condition || typeof condition !== 'object') return '?';
  const op = condition.op;
  if (op === 'always') return 'always';
  if (op === 'not' && condition.child?.op === 'replied') return 'if no reply';
  if (op === 'replied') return 'if replied';
  if (op === 'opened') return 'if opened';
  if (op === 'clicked') return 'if clicked';
  if (op === 'bounced') return 'if bounced';
  if (op === 'linkedin_connection') return `if LI=${condition.value || '?'}`;
  if (op === 'days_since_entered_node') return `≥${condition.gte ?? '?'}d here`;
  if (op === 'and' || op === 'or') return op;
  if (op === 'not') return 'not';
  return op || '?';
}

// --------------------------------------------------------------------------
// xyflow custom node renderer
// --------------------------------------------------------------------------

const HANDLE_STYLE = {
  width: 12,
  height: 12,
  border: '2px solid #3b82f6',
  background: 'white',
};

const KIND_COLORS = {
  email: 'bg-brand-500',
  email_reply: 'bg-indigo-500',
  wait: 'bg-amber-400',
  linkedin_view_profile: 'bg-sky-500',
  linkedin_follow_profile: 'bg-sky-500',
  linkedin_react_post: 'bg-sky-500',
};


function NodeCard({ data, selected }) {
  const dotColor = KIND_COLORS[data.kind] || 'bg-slate-400';
  const stats = data.stats;
  // Color the bottom border based on the success rate when we have stats.
  let statusBorder = '';
  if (stats && stats.attempted > 0) {
    const successRate = stats.sent / stats.attempted;
    statusBorder = successRate >= 0.7
      ? 'border-b-2 border-b-emerald-500'
      : successRate >= 0.3
      ? 'border-b-2 border-b-amber-500'
      : 'border-b-2 border-b-red-500';
  }
  return (
    <div
      className={`px-3 py-2 rounded-lg border bg-white shadow-sm min-w-[180px] relative ${statusBorder} ${
        selected ? 'border-brand-500 ring-2 ring-brand-200' : 'border-slate-200'
      }`}
    >
      {/* Target handle on the left — edges come INTO the node here. The
          entry node has no incoming edges, so we hide its target. */}
      {!data.isEntry && (
        <Handle
          type="target"
          position={Position.Left}
          style={HANDLE_STYLE}
          isConnectable={true}
        />
      )}
      <div className="flex items-center justify-between">
        <span className={`inline-block w-2 h-2 rounded-full mr-2 ${dotColor}`} />
        <span className="text-xs font-medium text-slate-700 uppercase tracking-wide">
          {data.kind}
        </span>
        {data.isEntry && (
          <span className="ml-2 text-[10px] font-semibold text-emerald-700 bg-emerald-100 rounded px-1.5 py-0.5">
            entry
          </span>
        )}
      </div>
      <div className="text-sm text-slate-900 mt-1">{data.title || '(unnamed)'}</div>
      {stats && (
        <div className="mt-1.5 pt-1.5 border-t border-slate-100 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-slate-600">
          <span title="Sent successfully" className="text-emerald-700">✓ {stats.sent}</span>
          {stats.skipped > 0 && (
            <span title="Skipped" className="text-slate-500">↷ {stats.skipped}</span>
          )}
          {stats.failed > 0 && (
            <span title="Failed" className="text-red-600">✗ {stats.failed}</span>
          )}
          {stats.currently_here > 0 && (
            <span title="Currently here" className="text-brand-600">⏳ {stats.currently_here}</span>
          )}
        </div>
      )}
      {/* Source handle on the right — drag from here to another node's left
          edge to create a connection. Always available, including on the
          entry node (which must have at least one outgoing edge to do work). */}
      <Handle
        type="source"
        position={Position.Right}
        style={HANDLE_STYLE}
        isConnectable={true}
      />
    </div>
  );
}

const NODE_TYPES = { card: NodeCard };

// --------------------------------------------------------------------------
// Side panel: per-node editor
// --------------------------------------------------------------------------

function NodeEditor({ node, onChange, onDelete, onMakeEntry }) {
  if (!node) {
    return (
      <div className="text-sm text-slate-500 p-4">
        Select a node to edit, or drag a new one from the palette.
      </div>
    );
  }
  const cfg = node.data.config || {};
  const setCfg = (patch) => onChange({ config: { ...cfg, ...patch } });

  return (
    <div className="p-4 space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <div className="font-semibold text-slate-900 capitalize">{node.data.kind} node</div>
        <div className="flex gap-2">
          {!node.data.isEntry && (
            <button
              type="button"
              onClick={onMakeEntry}
              className="text-xs px-2 py-1 rounded border border-slate-300 text-slate-600 hover:bg-slate-50"
            >
              Set as entry
            </button>
          )}
          <button
            type="button"
            onClick={onDelete}
            disabled={node.data.isEntry}
            className="text-xs px-2 py-1 rounded border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
            title={node.data.isEntry ? 'Cannot delete the entry node' : 'Delete this node'}
          >
            Delete
          </button>
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-slate-600 mb-1">Display title</label>
        <input
          value={node.data.title || ''}
          onChange={(e) => onChange({ title: e.target.value })}
          className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
          placeholder="e.g. Day-3 follow-up"
        />
      </div>

      {node.data.isEntry && node.data.kind !== 'email' && (
        <div className="p-3 bg-brand-50 rounded text-xs text-brand-700">
          This is the start node — the sequence begins here. No standalone
          first email is sent; the campaign launches straight into running.
          Add an email node downstream if you want to email these leads.
        </div>
      )}

      {node.data.kind === 'email' && (
        <>
          {node.data.isEntry ? (
            <div className="p-3 bg-slate-50 rounded text-xs text-slate-600">
              The entry email uses your campaign's compose pipeline output
              (the personalized email Claude wrote per lead). Subject + body
              are not editable here.
            </div>
          ) : (
            <>
              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">Subject template</label>
                <input
                  value={cfg.subject_template || ''}
                  onChange={(e) => setCfg({ subject_template: e.target.value })}
                  placeholder="Following up, {{first_name}}?"
                  className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">Body template</label>
                <textarea
                  value={cfg.body_template || ''}
                  onChange={(e) => setCfg({ body_template: e.target.value })}
                  rows={8}
                  placeholder={'Hi {{first_name}},\n\nJust following up on my last note...'}
                  className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                />
                <p className="mt-1 text-[11px] text-slate-500">
                  Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
                  <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>,{' '}
                  <code>{'{{email}}'}</code>.
                </p>
              </div>
            </>
          )}
        </>
      )}

      {node.data.kind === 'email_reply' && (
        <>
          <div className="p-3 bg-brand-50 rounded text-xs text-brand-700">
            Replies in-thread to the lead’s original campaign email (same
            subject, threaded — not a new conversation). Only fires once the
            first email has been sent.
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              data-testid="reply-ai-toggle"
              checked={!!cfg.ai_compose}
              onChange={(e) => setCfg({ ai_compose: e.target.checked })}
            />
            Let the AI write the reply
          </label>
          {cfg.ai_compose ? (
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                What should the follow-up say? (optional)
              </label>
              <textarea
                data-testid="reply-ai-prompt"
                value={cfg.ai_prompt || ''}
                onChange={(e) => setCfg({ ai_prompt: e.target.value })}
                rows={3}
                placeholder="e.g. Mention the free trial ends Friday and offer a 15-min call"
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
              />
              <p className="mt-1 text-[11px] text-slate-500">
                A generalized idea — the AI writes a short reply around it,
                reusing the lead’s existing research (no new research is done).
                Leave blank for a generic nudge.
              </p>
            </div>
          ) : (
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Reply body</label>
              <textarea
                data-testid="reply-body-template"
                value={cfg.body_template || ''}
                onChange={(e) => setCfg({ body_template: e.target.value })}
                rows={6}
                placeholder={'Hi {{first_name}},\n\nJust floating this back to the top of your inbox...'}
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
              />
              <p className="mt-1 text-[11px] text-slate-500">
                Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
                <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>.
              </p>
            </div>
          )}
        </>
      )}

      {node.data.kind === 'wait' && (() => {
        const totalMinutes = Number(cfg.duration_minutes ?? 4320);
        const { value, unit } = splitDuration(totalMinutes);
        const setMinutes = (m) => setCfg({ duration_minutes: Math.max(0, Math.floor(m)) });
        return (
          <div className="space-y-2">
            <label className="block text-xs font-medium text-slate-600">Duration</label>
            <div className="flex gap-2">
              <input
                type="number"
                min="0"
                value={value}
                onChange={(e) => setMinutes(Number(e.target.value) * unitMinutes(unit))}
                className="w-24 px-2 py-1.5 text-sm border border-slate-300 rounded"
              />
              <select
                value={unit}
                onChange={(e) => setMinutes(value * unitMinutes(e.target.value))}
                className="flex-1 px-2 py-1.5 text-sm border border-slate-300 rounded bg-white"
              >
                {WAIT_UNITS.map((u) => (
                  <option key={u.key} value={u.key}>{u.label}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {WAIT_PRESETS.map((p) => {
                const active = totalMinutes === p.minutes;
                return (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => setMinutes(p.minutes)}
                    className={`text-[11px] px-2 py-0.5 rounded border ${
                      active
                        ? 'bg-slate-900 text-white border-slate-900'
                        : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
                    }`}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>
            <p className="mt-1 text-[11px] text-slate-500">
              {totalMinutes <= 0
                ? 'No pause — the next step fires on the next scheduler tick.'
                : `Stored as ${totalMinutes} minute${totalMinutes === 1 ? '' : 's'}.`}
            </p>
          </div>
        );
      })()}

      {(node.data.kind === 'linkedin_view_profile'
        || node.data.kind === 'linkedin_follow_profile') && (
        <div className="p-3 bg-slate-50 rounded text-xs text-slate-600">
          Uses the lead's <code>linkedin_url</code>. The campaign must have a
          LinkedIn account configured (Settings → LinkedIn accounts). Leads
          with no LinkedIn URL are skipped automatically.
        </div>
      )}

      {node.data.kind === 'linkedin_react_post' && (
        <>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Reaction</label>
            <select
              value={(cfg.reaction || 'LIKE').toUpperCase()}
              onChange={(e) => setCfg({ reaction: e.target.value })}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded bg-white"
            >
              <option value="LIKE">Like</option>
              <option value="CELEBRATE">Celebrate</option>
              <option value="SUPPORT">Support</option>
              <option value="LOVE">Love</option>
              <option value="INSIGHTFUL">Insightful</option>
              <option value="FUNNY">Funny</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Post URN <span className="text-slate-400 font-normal">(optional)</span>
            </label>
            <input
              value={cfg.post_urn || ''}
              onChange={(e) => setCfg({ post_urn: e.target.value })}
              placeholder="urn:li:activity:..."
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
            />
            <p className="mt-1 text-[11px] text-slate-500">
              Leave blank to react to the lead's most recent post automatically.
            </p>
          </div>
        </>
      )}

      {node.data.kind === 'linkedin_connect' && (
        <>
          <div>
            <label className="flex items-center gap-2 text-xs font-medium text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={!!cfg.no_note}
                onChange={(e) => setCfg({ no_note: e.target.checked })}
                className="w-4 h-4 rounded border-slate-300 text-brand-600"
              />
              Send WITHOUT a note
            </label>
            <p className="mt-1 text-[11px] text-slate-500">
              Anecdotally, no-note invites have a higher accept rate lately.
            </p>
          </div>
          {!cfg.no_note && (
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                Note template <span className="text-slate-400 font-normal">(max 300 chars)</span>
              </label>
              <textarea
                value={cfg.note_template || ''}
                onChange={(e) => setCfg({ note_template: e.target.value })}
                rows={4}
                placeholder="Hi {{first_name}}, would love to connect."
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
              />
              <div className={`mt-1 text-[11px] ${(cfg.note_template || '').length > 300 ? 'text-red-600' : 'text-slate-500'}`}>
                {(cfg.note_template || '').length} / 300
              </div>
              <p className="mt-1 text-[11px] text-slate-500">
                Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
                <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>.
              </p>
            </div>
          )}
          <div className="p-3 bg-slate-50 rounded text-xs text-slate-600">
            Daily cap on connect requests is enforced server-side
            (default 15/day per account). Skipped invites land in
            <code className="ml-1">lead_step_executions</code> with the
            cap reason.
          </div>
        </>
      )}

      {node.data.kind === 'linkedin_dm' && (
        <>
          <div>
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={!!cfg.ai_compose}
                onChange={(e) => setCfg({ ai_compose: e.target.checked })}
                className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
              />
              <span className="text-xs font-medium text-slate-700">AI-compose (uses lead research data)</span>
            </label>
          </div>
          {cfg.ai_compose ? (
            <div className="p-3 bg-brand-50 border border-brand-100 rounded text-xs text-brand-800 leading-relaxed">
              Claude will write a personalized DM for each lead using the research data collected
              during ingest (LinkedIn headline, news, company context). Generated at send time —
              no preview until it fires.
            </div>
          ) : (
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Message template</label>
              <textarea
                value={cfg.text_template || ''}
                onChange={(e) => setCfg({ text_template: e.target.value })}
                rows={6}
                placeholder="Hi {{first_name}}, thanks for connecting!"
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
              />
              <p className="mt-1 text-[11px] text-slate-500">
                Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
                <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>.
              </p>
            </div>
          )}
          <p className="text-[11px] text-slate-500">
            Only fires when the lead is a 1st-degree connection — others are skipped automatically.
          </p>
        </>
      )}

      {node.data.kind === 'linkedin_invite_to_page' && (
        <>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Company page ID</label>
            <input
              value={cfg.page_id || ''}
              onChange={(e) => setCfg({ page_id: e.target.value.trim() })}
              placeholder="e.g. 12345678"
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
            />
            <p className="mt-1 text-[11px] text-slate-500">
              Numeric company ID — you can find it in the URL of your company
              page admin view. The connected LinkedIn account must be an admin
              of this page.
            </p>
          </div>
          <div className="p-3 bg-slate-50 rounded text-xs text-slate-600">
            Only fires for 1st-degree connections. LinkedIn enforces a monthly
            cap of ~250 page invites; we track it per-page in Redis.
          </div>
        </>
      )}

      {node.data.kind === 'linkedin_inmail' && (
        <>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Subject</label>
            <input
              value={cfg.subject_template || ''}
              onChange={(e) => setCfg({ subject_template: e.target.value })}
              placeholder="Quick question, {{first_name}}"
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Body template</label>
            <textarea
              value={cfg.body_template || ''}
              onChange={(e) => setCfg({ body_template: e.target.value })}
              rows={8}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
            />
            <p className="mt-1 text-[11px] text-slate-500">
              Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
              <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>.
            </p>
          </div>
          <div className="p-3 bg-amber-50 border border-amber-200 rounded text-xs text-amber-900">
            InMail requires LinkedIn <strong>Premium</strong> or <strong>Sales Navigator</strong>
            credits on the connected account. When credits run out, this step is
            skipped (not failed) and the campaign keeps moving.
          </div>
        </>
      )}

      {node.data.kind === 'linkedin_comment_post' && (
        <>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Comment template</label>
            <textarea
              value={cfg.comment_template || ''}
              onChange={(e) => setCfg({ comment_template: e.target.value })}
              rows={4}
              placeholder="Great point, {{first_name}} — really resonated."
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
            />
            <p className="mt-1 text-[11px] text-slate-500">
              Variables: <code>{'{{first_name}}'}</code>, <code>{'{{last_name}}'}</code>,{' '}
              <code>{'{{company}}'}</code>, <code>{'{{job_title}}'}</code>.
            </p>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Target post</label>
            <select
              value={cfg.target || 'latest'}
              onChange={(e) => setCfg({ target: e.target.value })}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded bg-white"
            >
              <option value="latest">Latest post</option>
              <option value="most_engaged">Most-engaged recent post</option>
            </select>
            <p className="mt-1 text-[11px] text-slate-500">
              Note: in M4 both options currently fall back to "latest" — most-engaged
              ranking ships when we extend the post fetch.
            </p>
          </div>
          <div className="p-3 bg-amber-50 border border-amber-200 rounded text-xs text-amber-900">
            Comments are publicly visible on the lead's post. Higher signal but
            higher risk — overuse looks bot-like to both the lead and LinkedIn.
            Recommend using it sparingly + handwriting the templates to sound
            human.
          </div>
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Visual condition builder (recursive)
// --------------------------------------------------------------------------

const CONDITION_OPS = [
  { value: 'always', label: 'always (default edge)' },
  { value: 'not', label: 'NOT (negate)' },
  { value: 'and', label: 'AND (all of)' },
  { value: 'or', label: 'OR (any of)' },
  { value: 'replied', label: 'replied' },
  { value: 'opened', label: 'opened' },
  { value: 'clicked', label: 'clicked' },
  { value: 'bounced', label: 'bounced' },
  { value: 'linkedin_connection', label: 'LinkedIn connection status' },
  { value: 'days_since_entered_node', label: 'days since entered node' },
];

const LI_CONNECTION_VALUES = ['unknown', 'invited', 'connected', 'declined', 'withdrawn'];

function defaultsForOp(op) {
  switch (op) {
    case 'always':       return { op: 'always' };
    case 'not':          return { op: 'not', child: { op: 'replied' } };
    case 'and':          return { op: 'and', children: [{ op: 'opened' }, { op: 'not', child: { op: 'replied' } }] };
    case 'or':           return { op: 'or', children: [{ op: 'replied' }, { op: 'bounced' }] };
    case 'replied':      return { op: 'replied', channels: ['email', 'linkedin'] };
    case 'opened':       return { op: 'opened' };
    case 'clicked':      return { op: 'clicked' };
    case 'bounced':      return { op: 'bounced' };
    case 'linkedin_connection':     return { op: 'linkedin_connection', value: 'connected' };
    case 'days_since_entered_node': return { op: 'days_since_entered_node', gte: 3 };
    default:             return { op: 'always' };
  }
}

function ConditionEditor({ condition, onChange, depth = 0 }) {
  const op = (condition && condition.op) || 'always';
  const borderColor =
    op === 'and' ? 'border-brand-300'
    : op === 'or' ? 'border-purple-300'
    : op === 'not' ? 'border-red-300'
    : 'border-slate-300';

  function setOp(newOp) {
    onChange(defaultsForOp(newOp));
  }

  function patchWithinDays(value) {
    const next = { ...condition };
    if (value === '' || value == null) delete next.within_days;
    else next.within_days = Number(value);
    onChange(next);
  }

  return (
    <div className={`border rounded p-2 bg-white ${borderColor}`}>
      <select
        value={op}
        onChange={(e) => setOp(e.target.value)}
        className="text-xs px-1.5 py-0.5 border border-slate-300 rounded bg-white font-medium"
      >
        {CONDITION_OPS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>

      {op === 'always' && (
        <div className="mt-1 text-[11px] text-slate-500 italic">
          No conditions — the lead always follows this edge.
        </div>
      )}

      {op === 'not' && (
        <div className="mt-2 pl-3 border-l-2 border-red-200">
          <ConditionEditor
            condition={condition.child || { op: 'always' }}
            onChange={(child) => onChange({ op: 'not', child })}
            depth={depth + 1}
          />
        </div>
      )}

      {(op === 'and' || op === 'or') && (
        <div className={`mt-2 pl-3 space-y-2 border-l-2 ${op === 'and' ? 'border-brand-200' : 'border-purple-200'}`}>
          {(condition.children || []).map((child, i) => (
            <div key={i} className="relative">
              <button
                type="button"
                title="Remove this branch"
                onClick={() => {
                  const next = [...condition.children];
                  next.splice(i, 1);
                  onChange({ ...condition, children: next });
                }}
                className="absolute -left-5 top-1 text-slate-300 hover:text-red-500 text-xs leading-none w-4 h-4 flex items-center justify-center"
              >
                ×
              </button>
              <ConditionEditor
                condition={child}
                onChange={(newChild) => {
                  const next = [...condition.children];
                  next[i] = newChild;
                  onChange({ ...condition, children: next });
                }}
                depth={depth + 1}
              />
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              onChange({
                ...condition,
                children: [...(condition.children || []), { op: 'always' }],
              })
            }
            className="text-[11px] text-brand-600 hover:underline"
          >
            + Add condition
          </button>
        </div>
      )}

      {op === 'replied' && (
        <div className="mt-2 space-y-1.5">
          <div className="text-[11px] text-slate-600">Channels:</div>
          <div className="flex gap-3 text-[11px]">
            {['email', 'linkedin'].map((ch) => {
              const channels = condition.channels || ['email', 'linkedin'];
              const checked = channels.includes(ch);
              return (
                <label key={ch} className="flex items-center gap-1 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => {
                      const cur = new Set(channels);
                      if (e.target.checked) cur.add(ch);
                      else cur.delete(ch);
                      onChange({
                        ...condition,
                        channels: [...cur],
                      });
                    }}
                    className="w-3.5 h-3.5"
                  />
                  {ch}
                </label>
              );
            })}
          </div>
          <div className="flex items-center gap-2 text-[11px]">
            <span className="text-slate-600">Within last</span>
            <input
              type="number"
              min="0"
              value={condition.within_days ?? ''}
              onChange={(e) => patchWithinDays(e.target.value)}
              className="w-16 px-1.5 py-0.5 border border-slate-300 rounded"
              placeholder="any"
            />
            <span className="text-slate-600">days (blank = ever)</span>
          </div>
        </div>
      )}

      {(op === 'opened' || op === 'clicked') && (
        <div className="mt-2 flex items-center gap-2 text-[11px]">
          <span className="text-slate-600">Within last</span>
          <input
            type="number"
            min="0"
            value={condition.within_days ?? ''}
            onChange={(e) => patchWithinDays(e.target.value)}
            className="w-16 px-1.5 py-0.5 border border-slate-300 rounded"
            placeholder="any"
          />
          <span className="text-slate-600">days (blank = ever)</span>
        </div>
      )}

      {op === 'linkedin_connection' && (
        <div className="mt-2 flex items-center gap-2 text-[11px]">
          <span className="text-slate-600">Status equals</span>
          <select
            value={condition.value || 'connected'}
            onChange={(e) => onChange({ op: 'linkedin_connection', value: e.target.value })}
            className="px-1.5 py-0.5 border border-slate-300 rounded bg-white"
          >
            {LI_CONNECTION_VALUES.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
      )}

      {op === 'days_since_entered_node' && (
        <div className="mt-2 flex items-center gap-2 text-[11px]">
          <span className="text-slate-600">≥</span>
          <input
            type="number"
            min="0"
            value={condition.gte ?? 0}
            onChange={(e) => onChange({ op: 'days_since_entered_node', gte: Number(e.target.value) })}
            className="w-16 px-1.5 py-0.5 border border-slate-300 rounded"
          />
          <span className="text-slate-600">days on this node</span>
        </div>
      )}
    </div>
  );
}


// --------------------------------------------------------------------------
// Side panel: per-edge editor
// --------------------------------------------------------------------------

function EdgeEditor({ edge, onChange, onDelete }) {
  const [showJson, setShowJson] = useState(false);
  const [jsonDraft, setJsonDraft] = useState('');

  if (!edge) return null;
  const cond = edge.data?.condition || { op: 'always' };

  function applyCondition(next) {
    onChange({
      data: { ...(edge.data || {}), condition: next },
      label: edgeLabel(next),
    });
  }

  return (
    <div className="p-4 space-y-3 text-sm border-t border-slate-200">
      <div className="flex items-center justify-between">
        <div className="font-semibold text-slate-900">Edge condition</div>
        <button
          type="button"
          onClick={onDelete}
          className="text-xs px-2 py-1 rounded border border-red-300 text-red-600 hover:bg-red-50"
        >
          Delete edge
        </button>
      </div>
      <p className="text-[11px] text-slate-500">
        When this evaluates to <code>true</code>, the lead follows this edge.
        Multiple edges from the same node? Lower priority wins.
      </p>

      <div className="flex items-center justify-between">
        <span className="text-[11px] text-slate-500">
          {showJson ? 'Raw JSON' : 'Visual builder'}
        </span>
        <button
          type="button"
          onClick={() => {
            if (!showJson) setJsonDraft(JSON.stringify(cond, null, 2));
            setShowJson((v) => !v);
          }}
          className="text-[11px] text-brand-600 hover:underline"
        >
          {showJson ? 'Switch to visual builder' : 'Show JSON'}
        </button>
      </div>

      {!showJson && (
        <ConditionEditor
          condition={cond}
          onChange={applyCondition}
        />
      )}

      {showJson && (
        <textarea
          value={jsonDraft}
          onChange={(e) => {
            setJsonDraft(e.target.value);
            try {
              const parsed = JSON.parse(e.target.value);
              applyCondition(parsed);
            } catch {
              // Mid-edit — don't propagate.
            }
          }}
          rows={8}
          className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
        />
      )}

      <div>
        <label className="block text-xs font-medium text-slate-600 mb-1">Priority</label>
        <input
          type="number"
          value={edge.data?.priority ?? 0}
          onChange={(e) => onChange({ data: { ...(edge.data || {}), priority: Number(e.target.value) } })}
          className="w-32 px-2 py-1.5 text-sm border border-slate-300 rounded"
        />
        <p className="mt-1 text-[11px] text-slate-500">
          When multiple outgoing edges match, the one with the LOWEST priority wins.
        </p>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Top-level builder
// --------------------------------------------------------------------------

function SequenceCanvas({ campaignId, embedded = false, onContinue = null, onSkip = null }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const flow = useReactFlow();
  const wrapperRef = useRef(null);

  const { data: remote, isLoading } = useQuery({
    queryKey: ['sequence', campaignId],
    queryFn: () => getSequence(campaignId),
  });
  const { data: analytics } = useQuery({
    queryKey: ['sequence-analytics', campaignId],
    queryFn: () => getSequenceAnalytics(campaignId),
    refetchInterval: 30000,  // 30s — cheap query, keeps the funnel live
  });

  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [selection, setSelection] = useState({ nodeId: null, edgeId: null });
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState([]);
  const [statusMsg, setStatusMsg] = useState(null);

  // Hydrate local state from the GET response. Stats merge in via a
  // separate effect below so analytics refetches don't clobber user edits.
  useEffect(() => {
    if (!remote) return;
    setNodes(
      (remote.nodes || []).map((n) => ({
        id: String(n.id),
        type: 'card',
        position: { x: n.position_x || 0, y: n.position_y || 0 },
        data: {
          kind: n.kind,
          config: n.config || {},
          isEntry: n.is_entry,
          stats: null,
          title:
            (n.config && n.config.title)
            || (n.kind === 'wait'
              ? formatWaitLabel(n.config?.duration_minutes)
              : n.is_entry
              ? 'Initial email'
              : 'Follow-up email'),
        },
      })),
    );
    setEdges(
      (remote.edges || []).map((e) => ({
        id: String(e.id),
        source: String(e.from_node_id),
        target: e.to_node_id ? String(e.to_node_id) : null,
        data: { condition: e.condition || { op: 'always' }, priority: e.priority || 0 },
        label: edgeLabel(e.condition),
        markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, color: '#475569' },
        style: { stroke: '#475569', strokeWidth: 1.5 },
        labelBgPadding: [6, 4],
        labelBgBorderRadius: 6,
        labelBgStyle: { fill: '#fff', stroke: '#cbd5e1' },
        labelStyle: { fill: '#334155', fontSize: 11 },
      })).filter((e) => e.target !== null),
    );
  }, [remote]);

  // Merge analytics stats onto existing nodes without resetting positions
  // or config (which the user may be mid-editing). Only touches nodes whose
  // stats actually changed — keeping the rest of the array reference-stable
  // matters because xyflow re-measures nodes on every new-reference render,
  // which used to trip "Maximum update depth exceeded" when analytics
  // refetched on its 30s interval.
  useEffect(() => {
    if (!analytics || !Array.isArray(analytics.per_node)) return;
    const statsByNode = {};
    for (const s of analytics.per_node) statsByNode[s.node_id] = s;
    setNodes((ns) => {
      let touched = false;
      const next = ns.map((n) => {
        const incoming = statsByNode[n.id] || null;
        if (statsEqual(n.data && n.data.stats, incoming)) return n;
        touched = true;
        return { ...n, data: { ...n.data, stats: incoming } };
      });
      // Return the SAME array reference when nothing actually changed so
      // React (and xyflow) skip the re-render entirely.
      return touched ? next : ns;
    });
  }, [analytics]);

  const onNodesChange = useCallback(
    (changes) => {
      setNodes((ns) => applyNodeChanges(changes, ns));
      // xyflow doesn't auto-clean edges when a node is removed.  Without
      // this, deleting a node leaves dangling edges with source/target
      // pointing at the removed node — they pass save() unchecked and
      // fail at publish-validate with a confusing error.
      const removed = new Set(
        changes.filter((c) => c.type === 'remove').map((c) => c.id),
      );
      if (removed.size) {
        setEdges((es) => es.filter(
          (e) => !removed.has(e.source) && !removed.has(e.target),
        ));
      }
    },
    [],
  );
  const onEdgesChange = useCallback((changes) => setEdges((es) => applyEdgeChanges(changes, es)), []);
  const onConnect = useCallback(
    (params) =>
      setEdges((es) => {
        // Default a new edge's condition based on what kind the source
        // node is.  For ``linkedin_connect`` we seed
        // {op: linkedin_connection, value: connected} so the downstream
        // step (DM, etc.) waits for the prospect to accept before firing —
        // the sequencer's "parking" behaviour relies on a deferrable
        // condition being on the edge.  Without this default the edge
        // would be ``always`` and the DM would fire on the next tick
        // whether the prospect accepted or not.
        const sourceNode = nodes.find((n) => n.id === params.source);
        const sourceKind = sourceNode?.data?.kind;
        let condition = { op: 'always' };
        let label = 'always';
        if (sourceKind === 'linkedin_connect') {
          condition = { op: 'linkedin_connection', value: 'connected' };
          label = 'if accepted';
        }
        return addEdge(
          {
            ...params,
            data: { condition, priority: 0 },
            label,
            markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, color: '#475569' },
            style: { stroke: '#475569', strokeWidth: 1.5 },
            labelBgPadding: [6, 4],
            labelBgBorderRadius: 6,
            labelBgStyle: { fill: '#fff', stroke: '#cbd5e1' },
            labelStyle: { fill: '#334155', fontSize: 11 },
          },
          es,
        );
      }),
    [nodes],
  );

  const onDragOver = useCallback((e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (e) => {
      e.preventDefault();
      const kind = e.dataTransfer.getData('application/sequence-kind');
      if (!kind) return;
      // screenToFlowPosition expects absolute page coordinates — do NOT
      // subtract the container offset, that double-shifts the position.
      const position = flow.screenToFlowPosition({
        x: e.clientX,
        y: e.clientY,
      });
      const id = `n_${Math.random().toString(36).slice(2, 10)}`;
      const defaults = defaultsForKind(kind);
      setNodes((ns) => [
        ...ns,
        {
          id,
          type: 'card',
          position,
          data: {
            kind,
            config: defaults.config,
            isEntry: ns.length === 0,
            title: defaults.title,
          },
        },
      ]);
    },
    [flow],
  );

  const selectedNode = nodes.find((n) => n.id === selection.nodeId) || null;
  const selectedEdge = edges.find((e) => e.id === selection.edgeId) || null;

  function patchNode(id, patch) {
    setNodes((ns) =>
      ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)),
    );
  }
  function patchEdge(id, patch) {
    setEdges((es) =>
      es.map((e) => (e.id === id ? { ...e, ...patch } : e)),
    );
  }
  function deleteNode(id) {
    const node = nodes.find((n) => n.id === id);
    if (node?.data.isEntry) return;
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
    setSelection({ nodeId: null, edgeId: null });
  }
  function deleteEdge(id) {
    setEdges((es) => es.filter((e) => e.id !== id));
    setSelection({ nodeId: null, edgeId: null });
  }
  function makeEntry(id) {
    setNodes((ns) =>
      ns.map((n) => ({ ...n, data: { ...n.data, isEntry: n.id === id } })),
    );
  }

  // Client-side preflight — catches structural issues (missing entry,
  // unreachable nodes, dangling edges) BEFORE we hit the backend, and
  // returns them in the same shape the publish validator does so the
  // error banner has one consistent renderer.
  const runClientPreflight = useCallback(() => {
    const errs = [];
    const nodeIds = new Set(nodes.map((n) => n.id));

    // Dangling edges (xyflow + our delete handler should prevent these
    // but defence-in-depth — there are still paths where a graph loaded
    // from a stale cache could carry one).
    const danglingEdges = edges.filter(
      (e) => !nodeIds.has(e.source) || !nodeIds.has(e.target),
    );
    danglingEdges.forEach((e) => {
      errs.push(`Edge ${e.id} points at a node that no longer exists — delete and redraw it.`);
    });

    // Entry node: must have exactly one.
    const entryNodes = nodes.filter((n) => n.data.isEntry);
    if (entryNodes.length === 0) {
      errs.push("No entry node. Mark one node as the entry (Set as entry button on the node card).");
    } else if (entryNodes.length > 1) {
      errs.push(`Multiple entry nodes (${entryNodes.length}). Only one node can be the entry.`);
    }

    // Reachability: every non-entry node must be reachable from an entry.
    if (entryNodes.length === 1 && nodes.length > 1) {
      const adj = new Map();
      edges.forEach((e) => {
        if (!adj.has(e.source)) adj.set(e.source, []);
        adj.get(e.source).push(e.target);
      });
      const reachable = new Set([entryNodes[0].id]);
      const stack = [entryNodes[0].id];
      while (stack.length) {
        const cur = stack.pop();
        for (const next of (adj.get(cur) || [])) {
          if (next && !reachable.has(next)) {
            reachable.add(next);
            stack.push(next);
          }
        }
      }
      nodes.forEach((n) => {
        if (!reachable.has(n.id)) {
          errs.push(`Node "${n.data.title || n.data.kind}" is unreachable from the entry — wire it in or delete it.`);
        }
      });
    }

    return errs;
  }, [nodes, edges]);

  const saveMut = useMutation({
    mutationFn: async () => {
      // Drop any orphan edges silently — the cleanup is purely about
      // making the payload acceptable.  Structural problems (missing
      // entry / unreachable nodes) are reported via runClientPreflight
      // on the Save / Publish buttons, not silently fixed.
      const nodeIds = new Set(nodes.map((n) => n.id));
      const liveEdges = edges.filter(
        (e) => nodeIds.has(e.source) && nodeIds.has(e.target),
      );
      const payload = {
        nodes: nodes.map((n) => ({
          client_id: n.id,
          kind: n.data.kind,
          config: { ...(n.data.config || {}), title: n.data.title },
          position_x: Math.round(n.position.x),
          position_y: Math.round(n.position.y),
          is_entry: !!n.data.isEntry,
        })),
        edges: liveEdges.map((e) => ({
          from_client_id: e.source,
          to_client_id: e.target,
          condition: e.data?.condition || { op: 'always' },
          priority: e.data?.priority || 0,
        })),
      };
      return await updateSequence(campaignId, payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sequence', campaignId] });
      setStatusMsg('Saved.');
      setErrors([]);
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail;
      setErrors(Array.isArray(detail) ? detail.map((d) => d?.msg || JSON.stringify(d)) : [String(detail || err.message)]);
    },
  });

  const validateMut = useMutation({
    mutationFn: () => validateSequence(campaignId),
    onSuccess: (r) => {
      setErrors(r.ok ? [] : r.errors || []);
      setStatusMsg(r.ok ? 'Sequence is valid.' : null);
    },
  });

  const publishMut = useMutation({
    mutationFn: () => publishSequence(campaignId),
    onSuccess: (r) => {
      if (r.ok) {
        const n = r.reenrolled_for_new_nodes || 0;
        setStatusMsg(
          n > 0
            ? `Published. Re-queued ${n} already-finished lead${n === 1 ? '' : 's'} for the new step(s).`
            : 'Published.',
        );
        setErrors([]);
        queryClient.invalidateQueries({ queryKey: ['sequence', campaignId] });
      } else {
        setErrors(r.errors || []);
      }
    },
  });

  if (isLoading) return <div className="p-8 text-slate-500">Loading sequence…</div>;

  return (
    <div className="flex h-[calc(100vh-4rem)]">
      {/* Palette */}
      <aside className="w-44 shrink-0 border-r border-slate-200 p-3 space-y-2 bg-white">
        <h3 className="text-xs font-semibold text-slate-700 uppercase tracking-wide">Palette</h3>
        {PALETTE.map((p) => (
          <div
            key={p.kind}
            draggable
            onDragStart={(e) => dragKind(e, p.kind)}
            className="px-3 py-2 rounded-lg border border-slate-200 bg-white cursor-grab hover:border-brand-400 active:cursor-grabbing"
          >
            <div className="text-sm font-medium text-slate-900">{p.label}</div>
            <div className="text-[11px] text-slate-500 mt-0.5">{p.hint}</div>
          </div>
        ))}
        <p className="text-[11px] text-slate-400 pt-2">
          Connect a LinkedIn account in Settings → LinkedIn accounts before
          using LI nodes.
        </p>
      </aside>

      {/* Canvas */}
      <div className="flex-1 relative" ref={wrapperRef} onDrop={onDrop} onDragOver={onDragOver}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onSelectionChange={({ nodes: sn, edges: se }) => {
            setSelection({
              nodeId: sn[0]?.id || null,
              edgeId: se[0]?.id || null,
            });
          }}
          fitView
          // Cap the zoom on initial fit so a graph with one node doesn't
          // load at zoom 4. Padding leaves room for the toolbar overlay.
          fitViewOptions={{ maxZoom: 1.0, padding: 0.2 }}
          minZoom={0.2}
          maxZoom={2.0}
        >
          <Background gap={16} />
          <Controls />
        </ReactFlow>
        <div className="absolute top-3 left-3 right-3 flex items-center gap-2 z-10">
          {embedded ? (
            <span className="px-3 py-1.5 text-sm font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg">
              Build your sequence
            </span>
          ) : (
            <Link
              to={`/campaigns/${campaignId}`}
              className="px-3 py-1.5 text-sm bg-white border border-slate-300 rounded-lg hover:bg-slate-50"
            >
              ← Back to campaign
            </Link>
          )}
          <span className="px-3 py-1.5 text-xs text-slate-600 bg-white/80 border border-slate-200 rounded-lg">
            Drag from the <span className="inline-block w-2 h-2 rounded-full border-2 border-brand-500 bg-white align-middle mx-1" />
            on a node's <strong>right edge</strong> to another node's <strong>left edge</strong> to connect them. Click an arrow to edit its condition.
          </span>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => {
              const pre = runClientPreflight();
              if (pre.length) {
                setErrors(pre);
                setStatusMsg(null);
                return;
              }
              saveMut.mutate();
            }}
            disabled={saveMut.isPending}
            className="px-3 py-1.5 text-sm bg-white border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-50"
          >
            {saveMut.isPending ? 'Saving…' : 'Save draft'}
          </button>
          <button
            type="button"
            onClick={() => {
              const pre = runClientPreflight();
              if (pre.length) {
                setErrors(pre);
                setStatusMsg(null);
                return;
              }
              validateMut.mutate();
            }}
            disabled={validateMut.isPending}
            className="px-3 py-1.5 text-sm bg-white border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-50"
          >
            Validate
          </button>
          {!embedded && (
            <button
              type="button"
              onClick={async () => {
                const pre = runClientPreflight();
                if (pre.length) {
                  setErrors(pre);
                  setStatusMsg(null);
                  return;
                }
                await saveMut.mutateAsync();
                publishMut.mutate();
              }}
              disabled={publishMut.isPending}
              className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50"
            >
              {publishMut.isPending ? 'Publishing…' : 'Save + Publish'}
            </button>
          )}
          {embedded && (
            <>
              <button
                type="button"
                onClick={onSkip}
                className="px-3 py-1.5 text-sm bg-white border border-slate-300 rounded-lg hover:bg-slate-50"
                title="Use the default 1-email sequence and skip building"
              >
                Skip — use default
              </button>
              <button
                type="button"
                onClick={async () => {
                  // Save + publish + advance the wizard. If preflight or
                  // publish fails, surface errors inline; don't advance.
                  const pre = runClientPreflight();
                  if (pre.length) {
                    setErrors(pre);
                    setStatusMsg(null);
                    return;
                  }
                  await saveMut.mutateAsync();
                  const result = await publishMut.mutateAsync();
                  if (result?.ok && onContinue) onContinue();
                }}
                disabled={saveMut.isPending || publishMut.isPending}
                className="px-3 py-1.5 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50"
              >
                {publishMut.isPending ? 'Publishing…' : 'Continue to upload →'}
              </button>
            </>
          )}
        </div>
        {/* Error banner — top of the canvas, prominent.  Single source
            of truth for both server-side and client-preflight errors. */}
        {errors.length > 0 && (
          <div className="absolute top-12 left-3 right-3 z-10">
            <div className="px-3 py-2 text-sm rounded-lg bg-red-50 border border-red-300 text-red-800 shadow-sm">
              <div className="font-semibold mb-1">
                {errors.length === 1 ? "1 issue to fix:" : `${errors.length} issues to fix:`}
              </div>
              <ul className="list-disc list-inside space-y-0.5 text-xs">
                {errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </div>
          </div>
        )}
        {statusMsg && errors.length === 0 && (
          <div className="absolute bottom-3 left-3 right-3 z-10 space-y-1">
            <div className="px-3 py-2 text-xs rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800">
              {statusMsg}
            </div>
          </div>
        )}
      </div>

      {/* Right panel */}
      <aside className="w-80 shrink-0 border-l border-slate-200 bg-white overflow-y-auto">
        <NodeEditor
          node={selectedNode}
          onChange={(patch) => selectedNode && patchNode(selectedNode.id, patch)}
          onDelete={() => selectedNode && deleteNode(selectedNode.id)}
          onMakeEntry={() => selectedNode && makeEntry(selectedNode.id)}
        />
        <EdgeEditor
          edge={selectedEdge}
          onChange={(patch) => selectedEdge && patchEdge(selectedEdge.id, patch)}
          onDelete={() => selectedEdge && deleteEdge(selectedEdge.id)}
        />
      </aside>
    </div>
  );
}

export function EmbeddedSequenceBuilder({ campaignId, onContinue, onSkip }) {
  return (
    <ReactFlowProvider>
      <SequenceCanvas
        campaignId={campaignId}
        embedded
        onContinue={onContinue}
        onSkip={onSkip}
      />
    </ReactFlowProvider>
  );
}

export default function SequenceBuilder() {
  const { id } = useParams();
  return (
    <ReactFlowProvider>
      <SequenceCanvas campaignId={id} />
    </ReactFlowProvider>
  );
}
