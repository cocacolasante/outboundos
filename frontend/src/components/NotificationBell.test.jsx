import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/agent.js', () => ({
  listNotifications: vi.fn(),
  markNotificationRead: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));

import * as api from '../api/agent.js';
import NotificationBell from './NotificationBell.jsx';

function renderBell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NotificationBell />
    </QueryClientProvider>,
  );
}

const NOTIF = {
  id: 'n1',
  kind: 'positive_reply',
  title: 'Positive reply from Jane Doe',
  body: 'Wants a call next week',
  read_at: null,
  emailed_at: null,
  created_at: '2026-06-12T10:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.listNotifications.mockResolvedValue({
    items: [NOTIF], total: 1, unread: 1, page: 1, page_size: 20, total_pages: 1,
  });
});

describe('NotificationBell', () => {
  it('shows the unread count badge', async () => {
    renderBell();
    expect(await screen.findByTestId('bell-unread-count')).toHaveTextContent('1');
  });

  it('opens the dropdown and lists notifications', async () => {
    const user = userEvent.setup();
    renderBell();
    await screen.findByTestId('bell-unread-count');
    await user.click(screen.getByTestId('bell-button'));
    expect(screen.getByTestId('bell-dropdown')).toBeInTheDocument();
    expect(screen.getByText('Positive reply from Jane Doe')).toBeInTheDocument();
  });

  it('mark-read and mark-all-read call the API', async () => {
    api.markNotificationRead.mockResolvedValue({ ...NOTIF, read_at: '2026-06-12T11:00:00Z' });
    api.markAllNotificationsRead.mockResolvedValue({ updated: 1 });
    const user = userEvent.setup();
    renderBell();
    await screen.findByTestId('bell-unread-count');
    await user.click(screen.getByTestId('bell-button'));

    await user.click(screen.getByTestId('notification-read-n1'));
    await waitFor(() => {
      // React Query 5 passes a context object as the 2nd arg — check the id only.
      expect(api.markNotificationRead).toHaveBeenCalled();
      expect(api.markNotificationRead.mock.calls[0][0]).toBe('n1');
    });

    await user.click(screen.getByTestId('bell-read-all'));
    await waitFor(() => expect(api.markAllNotificationsRead).toHaveBeenCalled());
  });

  it('no badge when everything is read', async () => {
    api.listNotifications.mockResolvedValue({
      items: [{ ...NOTIF, read_at: '2026-06-12T11:00:00Z' }],
      total: 1, unread: 0, page: 1, page_size: 20, total_pages: 1,
    });
    renderBell();
    await waitFor(() => expect(api.listNotifications).toHaveBeenCalled());
    expect(screen.queryByTestId('bell-unread-count')).toBeNull();
  });
});
