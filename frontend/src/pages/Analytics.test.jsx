import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Analytics from './Analytics.jsx';

// Mock Recharts — JSDOM doesn't render SVG charts. Stub each piece as a div
// so we can assert structure.
vi.mock('recharts', () => {
  const stub = (name) => ({ children }) =>
    `__RECHARTS_${name}__ ${children ?? ''}`;
  // Components are used as JSX; return functional components.
  const noop = ({ children }) => <div>{children}</div>;
  return {
    LineChart: ({ children }) => <div data-testid="line-chart">{children}</div>,
    Line: ({ dataKey }) => <div data-testid={`line-${dataKey}`} />,
    XAxis: noop,
    YAxis: noop,
    CartesianGrid: noop,
    Tooltip: noop,
    Legend: noop,
    ResponsiveContainer: noop,
  };
});

vi.mock('../api/campaigns.js', () => ({
  getAnalytics: vi.fn(),
  listCampaignLeads: vi.fn().mockResolvedValue({
    items: [], total: 0, page: 1, page_size: 50, total_pages: 0,
  }),
  listCampaignErrors: vi.fn().mockResolvedValue([]),
  retryFailedLeads: vi.fn(),
}));

vi.mock('../api/sequences.js', () => ({
  getSequenceAnalytics: vi.fn().mockResolvedValue({
    campaign_id: 'c1', sequence_id: 's1',
    total_leads: 0, halted: 0, completed: 0, active: 0, pending: 0,
    per_node: [],
  }),
}));

import * as api from '../api/campaigns.js';
import * as seqApi from '../api/sequences.js';

const BASE = {
  campaign_id: 'c1',
  overview: {
    total_leads: 10, sent: 10, delivered: 9, opened: 5, clicked: 2,
    replied: 1, bounced: 1, spam_complaints: 0, unsubscribed: 0,
  },
  rates: {
    open_rate: 0.5, click_rate: 0.2, reply_rate: 0.1,
    bounce_rate: 0.1, spam_rate: 0, unsub_rate: 0, delivery_rate: 0.9,
  },
  reply_tracking_enabled: true,
  timeline: [
    { date: '2026-05-10', opens: 2, clicks: 1, replies: 0 },
    { date: '2026-05-11', opens: 3, clicks: 1, replies: 1 },
  ],
  research_quality_breakdown: [
    { quality: 'rich', count: 6, open_rate: 0.7 },
    { quality: 'low', count: 4, open_rate: 0.2 },
  ],
  sender_reputation_score: 87,
  best_subject_lines: [
    { subject: 'Quick question', sent: 10, open_rate: 0.5 },
  ],
};

function renderAnalytics() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/campaigns/c1/analytics']}>
        <Routes>
          <Route path="/campaigns/:id/analytics" element={<Analytics />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getAnalytics.mockResolvedValue(BASE);
  api.listCampaignErrors.mockResolvedValue([]);
});

describe('Analytics page', () => {
  it('renders all overview metrics', async () => {
    renderAnalytics();
    await screen.findByTestId('metric-sent');
    expect(screen.getByTestId('metric-sent')).toHaveTextContent('10');
    expect(screen.getByTestId('metric-delivered')).toHaveTextContent('9');
    expect(screen.getByTestId('metric-open-rate')).toHaveTextContent('50.0%');
    expect(screen.getByTestId('metric-click-rate')).toHaveTextContent('20.0%');
  });

  it('renders second-row metrics with reply rate', async () => {
    renderAnalytics();
    await screen.findByTestId('metric-reply-rate');
    expect(screen.getByTestId('metric-reply-rate')).toHaveTextContent('10.0%');
    expect(screen.getByTestId('metric-bounce-rate')).toHaveTextContent('10.0%');
  });

  it('renders the sequence performance card with secondary steps', async () => {
    seqApi.getSequenceAnalytics.mockResolvedValue({
      campaign_id: 'c1', sequence_id: 's1',
      total_leads: 100, halted: 2, completed: 30, active: 60, pending: 8,
      per_node: [
        { node_id: 'n1', kind: 'email', title: null, is_entry: true,
          attempted: 100, sent: 95, skipped: 0, failed: 5, currently_here: 0 },
        { node_id: 'n2', kind: 'wait', title: null, is_entry: false,
          attempted: 0, sent: 0, skipped: 0, failed: 0, currently_here: 12 },
        { node_id: 'n3', kind: 'email_reply', title: 'Trial nudge', is_entry: false,
          attempted: 40, sent: 38, skipped: 2, failed: 0, currently_here: 0 },
      ],
    });
    renderAnalytics();
    const card = await screen.findByTestId('sequence-performance');
    const rows = within(card).getAllByTestId('seq-node-row');
    expect(rows).toHaveLength(3);
    // The secondary in-thread reply step is surfaced (not just the first email).
    expect(card.textContent).toContain('Trial nudge');
    expect(card.textContent).toContain('Reply (in-thread)');
  });

  it('hides the sequence performance card for a single-node sequence', async () => {
    seqApi.getSequenceAnalytics.mockResolvedValue({
      campaign_id: 'c1', sequence_id: 's1',
      total_leads: 10, halted: 0, completed: 0, active: 10, pending: 0,
      per_node: [
        { node_id: 'n1', kind: 'email', title: null, is_entry: true,
          attempted: 10, sent: 10, skipped: 0, failed: 0, currently_here: 0 },
      ],
    });
    renderAnalytics();
    await screen.findByTestId('metric-sent');
    expect(screen.queryByTestId('sequence-performance')).not.toBeInTheDocument();
  });

  it('reply rate shows -- when tracking disabled', async () => {
    api.getAnalytics.mockResolvedValue({ ...BASE, reply_tracking_enabled: false });
    renderAnalytics();
    await screen.findByTestId('metric-reply-rate');
    expect(screen.getByTestId('metric-reply-rate')).toHaveTextContent('--');
  });

  it('reputation card colors green when score ≥ 80', async () => {
    renderAnalytics();
    await screen.findByTestId('reputation-score');
    const node = screen.getByTestId('reputation-score');
    expect(node).toHaveTextContent('87');
    expect(node).toHaveStyle({ color: 'rgb(34, 197, 94)' });
  });

  it('reputation card colors red when score < 50', async () => {
    api.getAnalytics.mockResolvedValue({ ...BASE, sender_reputation_score: 40 });
    renderAnalytics();
    await screen.findByTestId('reputation-score');
    expect(screen.getByTestId('reputation-score')).toHaveStyle({ color: 'rgb(220, 38, 38)' });
  });

  it('shows reply-tracking banner when disabled', async () => {
    api.getAnalytics.mockResolvedValue({ ...BASE, reply_tracking_enabled: false });
    renderAnalytics();
    expect(await screen.findByTestId('reply-tracking-banner')).toBeInTheDocument();
  });

  it('omits the banner when reply tracking enabled', async () => {
    renderAnalytics();
    await screen.findByTestId('reputation-score');
    expect(screen.queryByTestId('reply-tracking-banner')).not.toBeInTheDocument();
  });

  it('renders quality breakdown with each tier', async () => {
    renderAnalytics();
    const breakdown = await screen.findByTestId('quality-breakdown');
    expect(breakdown).toHaveTextContent(/rich/i);
    expect(breakdown).toHaveTextContent(/generic/i);
  });

  it('renders best subjects table', async () => {
    renderAnalytics();
    const best = await screen.findByTestId('best-subjects');
    expect(best).toHaveTextContent('Quick question');
  });

  it('timeline shows replies line when tracking enabled', async () => {
    renderAnalytics();
    await screen.findByTestId('timeline-chart');
    expect(screen.getByTestId('line-opens')).toBeInTheDocument();
    expect(screen.getByTestId('line-clicks')).toBeInTheDocument();
    expect(screen.getByTestId('line-replies')).toBeInTheDocument();
  });

  it('timeline hides replies line when tracking disabled', async () => {
    api.getAnalytics.mockResolvedValue({ ...BASE, reply_tracking_enabled: false });
    renderAnalytics();
    await screen.findByTestId('timeline-chart');
    expect(screen.getByTestId('line-opens')).toBeInTheDocument();
    expect(screen.queryByTestId('line-replies')).not.toBeInTheDocument();
  });
});


describe('Failed-leads banner', () => {
  it('does not render when no leads have failed', async () => {
    api.listCampaignErrors.mockResolvedValue([]);
    renderAnalytics();
    await screen.findByTestId('metric-sent');
    expect(screen.queryByTestId('failed-leads-banner')).not.toBeInTheDocument();
  });

  it('renders banner with count when leads have failed', async () => {
    api.listCampaignErrors.mockResolvedValue([
      { lead_id: 'l1', email: 'a@x.com', failed_stage: 'research' },
      { lead_id: 'l2', email: 'b@x.com', failed_stage: 'send' },
    ]);
    renderAnalytics();
    const banner = await screen.findByTestId('failed-leads-banner');
    expect(banner).toHaveTextContent(/2.*failed/i);
    expect(screen.getByTestId('retry-failed-button')).toBeInTheDocument();
  });

  it('Retry button calls retryFailedLeads', async () => {
    api.listCampaignErrors.mockResolvedValue([
      { lead_id: 'l1', email: 'a@x.com', failed_stage: 'research' },
    ]);
    api.retryFailedLeads.mockResolvedValue({
      research_retried: 1, compose_retried: 0, send_retried: 0,
    });
    renderAnalytics();
    await screen.findByTestId('failed-leads-banner');
    const { fireEvent, waitFor } = await import('@testing-library/react');
    fireEvent.click(screen.getByTestId('retry-failed-button'));
    await waitFor(() => {
      expect(api.retryFailedLeads).toHaveBeenCalledWith('c1');
    });
  });
});
