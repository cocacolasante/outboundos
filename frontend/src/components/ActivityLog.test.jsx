import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/crm.js', () => ({
  listActivities: vi.fn(),
  createActivity: vi.fn(),
  updateActivity: vi.fn(),
  deleteActivity: vi.fn(),
}));

import * as api from '../api/crm.js';
import ActivityLog from './ActivityLog.jsx';
import { ToastProvider } from './Toast.jsx';

function renderLog(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <ActivityLog {...props} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 50, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listActivities.mockResolvedValue(paged([]));
});

describe('ActivityLog', () => {
  it('shows the empty state when no activities exist', async () => {
    renderLog({ leadId: 'l1' });
    await waitFor(() => expect(screen.getByTestId('activity-empty')).toBeInTheDocument());
  });

  it('logs a call with direction via the quick form', async () => {
    api.createActivity.mockResolvedValue({ id: 'a1' });
    const user = userEvent.setup();
    renderLog({ leadId: 'l1' });

    await user.click(await screen.findByTestId('log-activity-toggle'));
    await user.click(screen.getByTestId('activity-type-call'));
    await user.click(screen.getByLabelText(/Inbound/i));
    await user.type(screen.getByTestId('activity-subject'), 'Intro chat');
    await user.type(screen.getByTestId('activity-body'), '20 minutes, interested');
    await user.click(screen.getByTestId('activity-save-btn'));

    await waitFor(() => {
      expect(api.createActivity).toHaveBeenCalledWith({
        lead_id: 'l1',
        opportunity_id: null,
        activity_type: 'call',
        subject: 'Intro chat',
        body: '20 minutes, interested',
        direction: 'inbound',
        due_at: null,
      });
    });
  });

  it('task type shows the due-date input and includes due_at in the payload', async () => {
    api.createActivity.mockResolvedValue({ id: 'a2' });
    const user = userEvent.setup();
    renderLog({ opportunityId: 'o1' });

    await user.click(await screen.findByTestId('log-activity-toggle'));
    await user.click(screen.getByTestId('activity-type-task'));
    // Direction radios hidden for tasks.
    expect(screen.queryByLabelText(/Inbound/i)).toBeNull();
    const due = screen.getByTestId('activity-due-at');
    await user.type(screen.getByTestId('activity-subject'), 'Send proposal');
    // datetime-local value
    await user.type(due, '2026-06-20T10:00');
    await user.click(screen.getByTestId('activity-save-btn'));

    await waitFor(() => {
      const payload = api.createActivity.mock.calls[0][0];
      expect(payload.activity_type).toBe('task');
      expect(payload.opportunity_id).toBe('o1');
      expect(payload.lead_id).toBeNull();
      expect(payload.due_at).toContain('2026-06-20');
    });
  });

  it('renders activities and toggles a task complete', async () => {
    api.listActivities.mockResolvedValue(paged([
      {
        id: 't1', lead_id: 'l1', opportunity_id: null,
        activity_type: 'task', subject: 'Follow up',
        body: null, direction: null,
        due_at: '2026-06-15T12:00:00Z', completed_at: null,
        occurred_at: '2026-06-11T12:00:00Z', created_at: '2026-06-11T12:00:00Z',
      },
      {
        id: 'c1', lead_id: 'l1', opportunity_id: null,
        activity_type: 'call', subject: 'Intro', body: 'good chat',
        direction: 'outbound', due_at: null, completed_at: null,
        occurred_at: '2026-06-10T12:00:00Z', created_at: '2026-06-10T12:00:00Z',
      },
    ]));
    api.updateActivity.mockResolvedValue({ id: 't1' });

    const user = userEvent.setup();
    renderLog({ leadId: 'l1' });

    const taskRow = await screen.findByTestId('activity-row-t1');
    expect(within(taskRow).getByText('Follow up')).toBeInTheDocument();
    const callRow = screen.getByTestId('activity-row-c1');
    expect(within(callRow).getByText(/outbound/)).toBeInTheDocument();

    await user.click(screen.getByTestId('task-toggle-t1'));
    await waitFor(() => {
      expect(api.updateActivity).toHaveBeenCalledWith('t1', { completed: true });
    });
  });

  it('save is disabled with an empty subject', async () => {
    const user = userEvent.setup();
    renderLog({ leadId: 'l1' });
    await user.click(await screen.findByTestId('log-activity-toggle'));
    expect(screen.getByTestId('activity-save-btn')).toBeDisabled();
  });
});
