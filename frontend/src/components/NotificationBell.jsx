import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from '../api/agent.js';

const KIND_ICONS = {
  positive_reply: '🎯',
  reply: '✉️',
  task_due: '⏰',
  task_overdue: '🔴',
  stale_opportunity: '🥶',
  digest: '📋',
  agent_error: '⚠️',
};

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['agent-notifications'],
    queryFn: () => listNotifications({ page_size: 20 }),
    refetchInterval: 60_000,
  });

  const readMutation = useMutation({
    mutationFn: markNotificationRead,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['agent-notifications'] }),
  });
  const readAllMutation = useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['agent-notifications'] }),
  });

  const unread = data?.unread ?? 0;
  const items = data?.items || [];

  return (
    <div className="fixed top-4 right-6 z-40" data-testid="notification-bell">
      <button
        type="button"
        data-testid="bell-button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Notifications"
        className="relative bg-white border border-slate-200 rounded-full p-2.5 shadow-sm hover:bg-slate-50"
      >
        <svg className="w-5 h-5 text-slate-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
        </svg>
        {unread > 0 && (
          <span
            data-testid="bell-unread-count"
            className="absolute -top-1 -right-1 bg-red-500 text-white text-xs font-bold rounded-full min-w-5 h-5 px-1 flex items-center justify-center"
          >
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          data-testid="bell-dropdown"
          className="absolute right-0 mt-2 w-96 max-h-[28rem] overflow-auto bg-white border border-slate-200 rounded-xl shadow-lg"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100 sticky top-0 bg-white">
            <span className="font-semibold text-slate-800 text-sm">Notifications</span>
            {unread > 0 && (
              <button
                type="button"
                data-testid="bell-read-all"
                onClick={() => readAllMutation.mutate()}
                className="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
              >
                Mark all read
              </button>
            )}
          </div>
          {items.length === 0 && (
            <p className="px-4 py-6 text-sm text-slate-400 text-center m-0">
              Nothing yet — agent alerts land here.
            </p>
          )}
          <ul className="list-none m-0 p-0">
            {items.map((n) => (
              <li
                key={n.id}
                data-testid={`notification-item-${n.id}`}
                className={`px-4 py-3 border-b border-slate-50 ${n.read_at ? 'opacity-60' : 'bg-indigo-50/40'}`}
              >
                <div className="flex items-start gap-2">
                  <span className="text-base leading-5">{KIND_ICONS[n.kind] || '🔔'}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-slate-800 m-0 truncate">{n.title}</p>
                    {n.body && (
                      <p className="text-xs text-slate-500 m-0 mt-0.5 line-clamp-2 whitespace-pre-line">{n.body}</p>
                    )}
                    <p className="text-xs text-slate-400 m-0 mt-1">
                      {new Date(n.created_at).toLocaleString()}
                    </p>
                  </div>
                  {!n.read_at && (
                    <button
                      type="button"
                      data-testid={`notification-read-${n.id}`}
                      onClick={() => readMutation.mutate(n.id)}
                      title="Mark read"
                      className="text-xs text-slate-400 hover:text-indigo-600 shrink-0"
                    >
                      ✓
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
