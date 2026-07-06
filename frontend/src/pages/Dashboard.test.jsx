import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('recharts', () => {
  const noop = ({ children }) => <div>{children}</div>;
  return {
    BarChart: ({ children }) => <div data-testid="bar-chart">{children}</div>,
    Bar: () => <div />, CartesianGrid: noop, XAxis: noop, YAxis: noop,
    Tooltip: noop, Legend: noop, ResponsiveContainer: noop,
    LineChart: ({ children }) => <div>{children}</div>, Line: () => <div />,
    PieChart: ({ children }) => <div>{children}</div>, Pie: () => <div />, Cell: () => <div />,
  };
});

vi.mock('../api/reports.js', () => ({ getReportOverview: vi.fn() }));
vi.mock('../api/campaigns.js', () => ({ listCampaigns: vi.fn() }));
vi.mock('../api/reportBuilder.js', () => ({ listReports: vi.fn(), runSavedReport: vi.fn() }));

import * as reportsApi from '../api/reports.js';
import * as campaignsApi from '../api/campaigns.js';
import * as builderApi from '../api/reportBuilder.js';
import Dashboard from './Dashboard.jsx';

const OVERVIEW = {
  start: '2026-03-01', end: '2026-06-01',
  kpis: {
    won_count: 3, won_value: 45000, lost_count: 1, lost_value: 5000,
    win_rate: 0.75, avg_deal_size: 15000, avg_sales_cycle_days: 21,
    open_count: 5, open_value: 80000, open_weighted_value: 32000,
    deals_created: 8, new_leads: 40, conversions: 6, activities_logged: 22,
  },
  won_lost_monthly: [], forecast: [], loss_reasons: [],
  pipeline_by_stage: [
    { stage: 'prospecting', count: 2, total_amount: 10000, weighted_amount: 1000 },
    { stage: 'proposal', count: 3, total_amount: 70000, weighted_amount: 35000 },
  ],
  activity_breakdown: { by_type: {}, by_direction: {}, agent_generated: 0, human_logged: 0, total: 0 },
  funnel: { new_leads: 40, opportunities_created: 8, won: 3, lead_to_opp_rate: 0.2, opp_to_won_rate: 0.375 },
};

const CAMPAIGNS = [
  { id: 'c1', name: 'A', stats: { sent_count: 100, opened: 50, replied: 5 } },
  { id: 'c2', name: 'B', stats: { sent_count: 100, opened: 30, replied: 5 } },
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  reportsApi.getReportOverview.mockResolvedValue(OVERVIEW);
  campaignsApi.listCampaigns.mockResolvedValue(CAMPAIGNS);
  builderApi.listReports.mockResolvedValue([]);
});

describe('Dashboard', () => {
  it('renders CRM KPIs from the overview', async () => {
    renderPage();
    await screen.findByTestId('dashboard');
    expect(await screen.findByTestId('metric-win-rate')).toHaveTextContent('75.0%');
    expect(screen.getByTestId('metric-won-this-period')).toHaveTextContent(/\$45,000/);
    expect(screen.getByTestId('metric-weighted-pipeline')).toHaveTextContent(/\$32,000/);
  });

  it('aggregates campaign metrics across campaigns', async () => {
    renderPage();
    // open rate = (50+30)/(100+100) = 40%, reply rate = 10/200 = 5%
    expect(await screen.findByTestId('metric-open-rate')).toHaveTextContent('40.0%');
    expect(screen.getByTestId('metric-reply-rate')).toHaveTextContent('5.0%');
    expect(screen.getByTestId('metric-emails-sent')).toHaveTextContent('200');
  });

  it('renders the pipeline chart', async () => {
    renderPage();
    expect(await screen.findByTestId('dashboard-pipeline-chart')).toBeInTheDocument();
  });

  it('shows an empty state when there are no saved reports', async () => {
    renderPage();
    const panel = await screen.findByTestId('dashboard-saved-reports');
    expect(within(panel).getByText(/No saved reports yet/i)).toBeInTheDocument();
  });

  it('runs a saved report and shows its result inline', async () => {
    builderApi.listReports.mockResolvedValue([
      { id: 'r1', name: 'Deals by stage', data_source: 'opportunities', description: null },
    ]);
    builderApi.runSavedReport.mockResolvedValue({
      columns: [{ key: 'stage', label: 'Stage', type: 'enum' }, { key: 'count', label: 'Count', type: 'number' }],
      rows: [{ stage: 'proposal', count: 3 }],
      row_count: 1, grouped: true, truncated: false, limit: 1000,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('dashboard-report-r1'));
    await waitFor(() => expect(builderApi.runSavedReport).toHaveBeenCalledWith('r1'));
    const result = await screen.findByTestId('saved-report-result');
    expect(within(result).getByText('proposal')).toBeInTheDocument();
  });

  it('shows an error state when the overview fails', async () => {
    reportsApi.getReportOverview.mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByTestId('error-state')).toBeInTheDocument();
  });
});
