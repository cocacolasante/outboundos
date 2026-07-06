import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/icp.js', () => ({
  getIcpProfile: vi.fn(),
  regenerateIcpProfile: vi.fn(),
  updateIcpCriteria: vi.fn(),
  listCandidates: vi.fn(),
  acceptCandidate: vi.fn(),
  rejectCandidate: vi.fn(),
}));

import * as api from '../api/icp.js';
import Lookalikes from './Lookalikes.jsx';
import { ToastProvider } from '../components/Toast.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <Lookalikes />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const PROFILE = {
  id: 'p1', name: 'Auto ICP', source: 'auto_closed_won', status: 'ready',
  criteria: {
    industries: ['managed IT services'],
    employee_count_band: '20-200',
    title_patterns: ['CFO'],
    geographies: [], funding_stages: [], keywords: ['compliance'],
  },
  won_deal_count: 4, refreshed_at: '2026-06-12T02:00:00Z', min_won_deals: 3,
};

const CANDIDATE = {
  id: 'c1', company: 'FreshCo', company_website: 'https://freshco.io',
  contact_name: 'Casey Doe', job_title: 'CFO', linkedin_url: null,
  email: 'casey@freshco.io', fit_score: 85,
  fit_reason: 'industry match (managed IT); 50 employees in band 20-200',
  source: 'apollo_people', status: 'new', created_lead_id: null,
  created_at: '2026-06-12T03:00:00Z',
};

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 50, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getIcpProfile.mockResolvedValue(PROFILE);
  api.listCandidates.mockResolvedValue(paged([CANDIDATE]));
});

describe('Lookalikes page', () => {
  it('renders the ICP summary and the ranked candidate row', async () => {
    renderPage();
    const card = await screen.findByTestId('icp-card');
    expect(card).toHaveTextContent('4 closed-won deals');
    expect(within(card).getByTestId('icp-criteria')).toHaveTextContent('managed IT services');

    const row = await screen.findByTestId('candidate-row-c1');
    expect(within(row).getByTestId('fit-score-c1')).toHaveTextContent('85');
    expect(row).toHaveTextContent('FreshCo');
    expect(row).toHaveTextContent('industry match');
  });

  it('accept calls the API and refreshes', async () => {
    api.acceptCandidate.mockResolvedValue({ ...CANDIDATE, status: 'accepted', lead_id: 'l1' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('accept-c1'));
    await waitFor(() => {
      expect(api.acceptCandidate).toHaveBeenCalled();
      expect(api.acceptCandidate.mock.calls[0][0]).toBe('c1');
    });
  });

  it('reject calls the API', async () => {
    api.rejectCandidate.mockResolvedValue({ ...CANDIDATE, status: 'rejected' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('reject-c1'));
    await waitFor(() => expect(api.rejectCandidate).toHaveBeenCalled());
  });

  it('insufficient-data profile shows the threshold hint and empty state', async () => {
    api.getIcpProfile.mockResolvedValue({
      ...PROFILE, status: 'insufficient_data', won_deal_count: 1, criteria: {},
    });
    api.listCandidates.mockResolvedValue(paged([]));
    renderPage();
    const card = await screen.findByTestId('icp-card');
    expect(card).toHaveTextContent(/Needs ≥ 3 closed-won deals/);
    expect(await screen.findByTestId('candidates-empty')).toBeInTheDocument();
  });
});
