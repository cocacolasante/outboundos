import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createActivity,
  deleteActivity,
  listActivities,
  updateActivity,
} from '../api/crm.js';
import { useToast } from './Toast.jsx';

const TYPE_META = {
  call: { label: 'Call', icon: '📞', hasDirection: true },
  email: { label: 'Email', icon: '📧', hasDirection: true },
  meeting: { label: 'Meeting', icon: '📅', hasDirection: false },
  note: { label: 'Note', icon: '📝', hasDirection: false },
  task: { label: 'Task', icon: '☑️', hasDirection: false },
};

function fmt(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

/**
 * Shared CRM activity log — renders the logged calls / emails /
 * meetings / notes / tasks for ONE parent (a lead or an opportunity)
 * plus a quick-log form.  Used by both the lead modal and the
 * opportunity detail.
 *
 * Props: exactly one of `leadId` / `opportunityId` (or both for
 * conversion-spanning logs).
 */
export default function ActivityLog({ leadId, opportunityId }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const params = {};
  if (leadId) params.lead_id = leadId;
  if (opportunityId) params.opportunity_id = opportunityId;
  const queryKey = ['crm-activities', leadId || null, opportunityId || null];

  const { data: page } = useQuery({
    queryKey,
    queryFn: () => listActivities(params),
  });
  const activities = page?.items ?? [];

  // ---- quick-log form state ----
  const [type, setType] = useState('note');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [direction, setDirection] = useState('outbound');
  const [dueAt, setDueAt] = useState('');
  const [open, setOpen] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey });
    // The lead history timeline merges these — refresh it too.
    if (leadId) queryClient.invalidateQueries({ queryKey: ['lead-detail-v2', leadId] });
    if (opportunityId) {
      queryClient.invalidateQueries({ queryKey: ['crm-opportunity', opportunityId] });
      queryClient.invalidateQueries({ queryKey: ['crm-opportunities'] });
    }
  };

  const logMut = useMutation({
    mutationFn: () => createActivity({
      lead_id: leadId || null,
      opportunity_id: opportunityId || null,
      activity_type: type,
      subject: subject.trim(),
      body: body.trim() || null,
      direction: TYPE_META[type].hasDirection ? direction : null,
      due_at: type === 'task' && dueAt ? new Date(dueAt).toISOString() : null,
    }),
    onSuccess: () => {
      invalidate();
      setSubject('');
      setBody('');
      setDueAt('');
      setOpen(false);
      toast.success('Activity logged');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to log activity'),
  });

  const toggleTaskMut = useMutation({
    mutationFn: ({ id, completed }) => updateActivity(id, { completed }),
    onSuccess: invalidate,
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to update task'),
  });

  const deleteMut = useMutation({
    mutationFn: (id) => deleteActivity(id),
    onSuccess: invalidate,
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  return (
    <div data-testid="activity-log">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
          Logged activities
        </span>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          data-testid="log-activity-toggle"
          className="px-2.5 py-1 text-xs font-medium bg-brand-600 hover:bg-brand-700 text-white rounded-md"
        >
          {open ? 'Cancel' : '+ Log activity'}
        </button>
      </div>

      {open && (
        <div
          data-testid="log-activity-form"
          className="bg-slate-50 border border-slate-200 rounded-lg p-3 mb-3 space-y-2"
        >
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(TYPE_META).map(([value, meta]) => (
              <button
                key={value}
                type="button"
                onClick={() => setType(value)}
                data-testid={`activity-type-${value}`}
                aria-pressed={type === value}
                className={`px-2.5 py-1 text-xs font-medium rounded-md border ${
                  type === value
                    ? 'bg-brand-600 text-white border-brand-600'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-100'
                }`}
              >
                {meta.icon} {meta.label}
              </button>
            ))}
          </div>
          {TYPE_META[type].hasDirection && (
            <div className="flex gap-3 text-xs text-slate-600">
              <label className="flex items-center gap-1">
                <input
                  type="radio" name="direction" value="outbound"
                  checked={direction === 'outbound'}
                  onChange={() => setDirection('outbound')}
                /> Outbound
              </label>
              <label className="flex items-center gap-1">
                <input
                  type="radio" name="direction" value="inbound"
                  checked={direction === 'inbound'}
                  onChange={() => setDirection('inbound')}
                /> Inbound
              </label>
            </div>
          )}
          <input
            type="text"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder={type === 'task' ? 'Task — e.g. "Send proposal"' : 'Subject'}
            data-testid="activity-subject"
            className="w-full px-3 py-1.5 border border-slate-300 rounded-md text-sm bg-white"
          />
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={2}
            placeholder="Details (optional)"
            data-testid="activity-body"
            className="w-full px-3 py-1.5 border border-slate-300 rounded-md text-sm bg-white"
          />
          {type === 'task' && (
            <label className="flex items-center gap-2 text-xs text-slate-600">
              Due
              <input
                type="datetime-local"
                value={dueAt}
                onChange={(e) => setDueAt(e.target.value)}
                data-testid="activity-due-at"
                className="px-2 py-1 border border-slate-300 rounded-md text-xs bg-white"
              />
            </label>
          )}
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => logMut.mutate()}
              disabled={!subject.trim() || logMut.isPending}
              data-testid="activity-save-btn"
              className="px-3 py-1.5 text-xs font-medium bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white rounded-md"
            >
              {logMut.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {activities.length === 0 ? (
        <p className="text-sm text-slate-400 italic m-0" data-testid="activity-empty">
          No logged activities yet.
        </p>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-lg overflow-hidden">
          {activities.map((a) => {
            const meta = TYPE_META[a.activity_type] || { label: a.activity_type, icon: '•' };
            const isTask = a.activity_type === 'task';
            const overdue = isTask && !a.completed_at && a.due_at
              && new Date(a.due_at) < new Date();
            return (
              <div
                key={a.id}
                data-testid={`activity-row-${a.id}`}
                className="flex items-start gap-2.5 px-3 py-2 text-sm bg-white"
              >
                {isTask ? (
                  <input
                    type="checkbox"
                    checked={!!a.completed_at}
                    onChange={(e) => toggleTaskMut.mutate({ id: a.id, completed: e.target.checked })}
                    data-testid={`task-toggle-${a.id}`}
                    className="mt-0.5"
                    title={a.completed_at ? 'Reopen task' : 'Mark complete'}
                  />
                ) : (
                  <span className="text-base leading-tight">{meta.icon}</span>
                )}
                <div className="flex-1 min-w-0">
                  <div className={`font-medium ${a.completed_at && isTask ? 'line-through text-slate-400' : 'text-slate-900'}`}>
                    {a.subject}
                    {a.direction && (
                      <span className="ml-1.5 text-xs font-normal text-slate-400">
                        ({a.direction})
                      </span>
                    )}
                    {a.is_agent_generated && (
                      <span
                        data-testid={`ai-tag-${a.id}`}
                        title="Logged automatically by the agent"
                        className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-100 text-violet-700 align-middle"
                      >
                        AI
                      </span>
                    )}
                    {a.sentiment && (
                      <span
                        className={`ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold align-middle ${
                          a.sentiment === 'positive'
                            ? 'bg-emerald-100 text-emerald-700'
                            : a.sentiment === 'negative'
                              ? 'bg-red-100 text-red-700'
                              : 'bg-slate-100 text-slate-500'
                        }`}
                      >
                        {a.sentiment}
                      </span>
                    )}
                  </div>
                  {a.body && (
                    <div className="text-xs text-slate-500 mt-0.5 whitespace-pre-wrap">{a.body}</div>
                  )}
                  <div className="text-xs text-slate-400 mt-0.5">
                    {meta.label} · {fmt(a.occurred_at)}
                    {isTask && a.due_at && (
                      <span className={overdue ? 'text-red-600 font-medium' : ''}>
                        {' '}· due {fmt(a.due_at)}{overdue ? ' (overdue)' : ''}
                      </span>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => deleteMut.mutate(a.id)}
                  className="text-slate-300 hover:text-red-500 text-xs"
                  aria-label="Delete activity"
                  title="Delete"
                >✕</button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
