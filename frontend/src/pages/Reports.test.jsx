import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/reports.js', () => ({
  getReportOverview: vi.fn(),
  getReportDeals: vi.fn(),
  getReportActivities: vi.fn(),
}));

vi.mock('recharts', () => {
  const noop = ({ children }) => <div>{children}</div>;
  return {
    BarChart: ({ children }) => <div data-testid="bar-chart">{children}</div>,
    Bar: () => <div />,
    XAxis: noop, YAxis: noop, CartesianGrid: noop, Tooltip: noop,
    Legend: noop, ResponsiveContainer: noop,
  };
});

import * as api from '../api/reports.js';
import Reports from './Reports.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Reports />
    </QueryClientProvider>,
  );
}

const OVERVIEW = {
  start: '2026-03-16', end: '2026-06-14',
  kpis: {
    won_count: 3, won_value: 45000, lost_count: 2, lost_value: 8000,
    win_rate: 0.6, avg_deal_size: 15000, avg_sales_cycle_days: 21.5,
    open_count: 4, open_value: 60000, open_weighted_value: 28000,
    deals_created: 6, new_leads: 20, conversions: 5, activities_logged: 42,
  },
  won_lost_monthly: [
    { month: '2026-04', won_count: 1, won_value: 15000, lost_count: 1, lost_value: 3000 },
    { month: '2026-05', won_count: 2, won_value: 30000, lost_count: 1, lost_value: 5000 },
  ],
  pipeline_by_stage: [
    { stage: 'proposal', count: 2, total_amount: 30000, weighted_amount: 15000 },
    { stage: 'negotiation', count: 2, total_amount: 30000, weighted_amount: 22500 },
  ],
  forecast: [
    { month: '2026-07', count: 3, total_amount: 40000, weighted_amount: 20000 },
    { month: 'unscheduled', count: 1, total_amount: 20000, weighted_amount: 8000 },
  ],
  loss_reasons: [
    { reason: 'Price', count: 2, value: 8000 },
  ],
  activity_breakdown: {
    by_type: { call: 10, email: 25, note: 7 },
    by_direction: { outbound: 20, inbound: 15 },
    agent_generated: 12, human_logged: 30, total: 42,
  },
  funnel: {
    new_leads: 20, opportunities_created: 6, won: 3,
    lead_to_opp_rate: 0.3, opp_to_won_rate: 0.5,
  },
};

const DEALS_WON = {
  outcome: 'won', start: '2026-03-16', end: '2026-06-14',
  count: 1, total_amount: 15000, truncated: false,
  items: [{
    id: 'd1', name: 'Acme expansion', stage: 'closed_won', amount: 15000,
    probability: 100, weighted_amount: 15000, company: 'Acme',
    contact_name: 'Jane Doe', email: 'jane@acme.io',
    created_at: '2026-04-01T00:00:00Z', closed_at: '2026-04-22T00:00:00Z',
    close_date: '2026-04-22', age_days: 21, loss_reason: null,
  }],
};

const ACTIVITIES = {
  start: '2026-03-16', end: '2026-06-14', count: 1, truncated: false,
  items: [{
    id: 'a1', activity_type: 'call', direction: 'outbound', subject: 'Discovery call',
    occurred_at: '2026-06-10T15:00:00Z', due_at: null, completed_at: null,
    is_agent_generated: false, sentiment: null,
    lead_id: null, opportunity_id: 'o1', opportunity_name: 'Acme expansion',
    contact: 'jane@acme.io',
  }],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getReportOverview.mockResolvedValue(OVERVIEW);
  api.getReportDeals.mockResolvedValue(DEALS_WON);
  api.getReportActivities.mockResolvedValue(ACTIVITIES);
});

describe('Reports page', () => {
  it('renders KPI cards from the overview', async () => {
    renderPage();
    const grid = await screen.findByTestId('kpi-grid');
    expect(within(grid).getByText('$45,000')).toBeInTheDocument();   // won value
    expect(within(grid).getByText('60.0%')).toBeInTheDocument();     // win rate
    expect(within(grid).getByText('21.5 days')).toBeInTheDocument(); // avg cycle
  });

  it('renders pipeline, forecast, loss reasons, funnel, activity breakdown', async () => {
    renderPage();
    const pipeline = await screen.findByTestId('pipeline-card');
    expect(pipeline).toHaveTextContent('Proposal');
    expect(pipeline).toHaveTextContent('$15,000');  // weighted

    expect(screen.getByTestId('forecast-card')).toHaveTextContent('No close date');
    expect(screen.getByTestId('loss-reasons-card')).toHaveTextContent('Price');
    expect(within(screen.getByTestId('funnel-rows')).getByText('Won')).toBeInTheDocument();
    expect(screen.getByTestId('activity-breakdown')).toHaveTextContent('email');
  });

  it('default preset is 90 days; switching presets refetches with new start', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('kpi-grid');
    expect(screen.getByTestId('preset-90')).toHaveAttribute('aria-pressed', 'true');

    api.getReportOverview.mockClear();
    await user.click(screen.getByTestId('preset-all'));
    await waitFor(() => {
      expect(api.getReportOverview).toHaveBeenCalled();
      const params = api.getReportOverview.mock.calls.at(-1)[0];
      expect(params.start).toBe('2000-01-01');
    });
  });

  it('deal tabs switch the outcome query', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('deals-detail');
    // Default outcome won.
    await waitFor(() => {
      expect(api.getReportDeals.mock.calls.some((c) => c[0].outcome === 'won')).toBe(true);
    });
    await user.click(screen.getByTestId('deal-tab-lost'));
    await waitFor(() => {
      expect(api.getReportDeals.mock.calls.some((c) => c[0].outcome === 'lost')).toBe(true);
    });
  });

  it('exports deal rows to CSV', async () => {
    const clickSpy = vi.fn();
    const origCreate = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = origCreate(tag);
      if (tag === 'a') el.click = clickSpy;
      return el;
    });
    // jsdom has no URL.createObjectURL — stub it.
    const urlSpy = vi.fn().mockReturnValue('blob:x');
    URL.createObjectURL = urlSpy;
    URL.revokeObjectURL = vi.fn();

    const user = userEvent.setup();
    renderPage();
    const deals = await screen.findByTestId('deals-detail');
    await waitFor(() => expect(within(deals).getByTestId('deals-table')).toBeInTheDocument());
    await user.click(within(deals).getByTestId('export-csv-btn'));

    expect(urlSpy).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();

    document.createElement.mockRestore();
  });

  it('renders the activity detail table', async () => {
    renderPage();
    const table = await screen.findByTestId('activities-table');
    expect(table).toHaveTextContent('Discovery call');
    expect(table).toHaveTextContent('Acme expansion');
  });
});
