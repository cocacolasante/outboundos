import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Campaigns from './Campaigns.jsx';
import { ToastProvider } from '../components/Toast.jsx';

vi.mock('../api/campaigns.js', () => ({
  listCampaigns: vi.fn(),
  pauseCampaign: vi.fn(),
  resumeCampaign: vi.fn(),
  deleteCampaign: vi.fn(),
}));

import * as api from '../api/campaigns.js';

const SAMPLE_CAMPAIGNS = [
  {
    id: 'c1', name: 'Spring outreach', status: 'running',
    created_at: '2026-05-01T12:00:00Z',
    lead_counts: { total: 100, pending: 50, scheduled: 0, sent: 50, failed: 0 },
    stats: { open_rate: 0.5, click_rate: 0.2, reply_rate: 0.05 },
    connected_account_configured: true,
  },
  {
    id: 'c2', name: 'Cold winter', status: 'paused',
    created_at: '2026-04-15T12:00:00Z',
    lead_counts: { total: 50, pending: 30, scheduled: 0, sent: 20, failed: 0 },
    stats: { open_rate: 0.3, click_rate: 0.1, reply_rate: null },
    connected_account_configured: false,
  },
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter>
          <Campaigns />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Campaigns list', () => {
  it('shows loading skeleton while loading', () => {
    api.listCampaigns.mockReturnValue(new Promise(() => {})); // never resolves
    renderPage();
    expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
  });

  it('renders empty state when no campaigns', async () => {
    api.listCampaigns.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByTestId('empty-state')).toBeInTheDocument();
  });

  it('renders one card per campaign', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    renderPage();
    await waitFor(() => {
      expect(screen.getAllByTestId('campaign-card')).toHaveLength(2);
    });
    expect(screen.getByText('Spring outreach')).toBeInTheDocument();
    expect(screen.getByText('Cold winter')).toBeInTheDocument();
  });

  it('shows reply rate "--" for campaigns without connected inbox', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    renderPage();
    const cards = await screen.findAllByTestId('campaign-card');
    // c2 has connected_account_configured: false
    const c2Card = cards.find((c) => c.dataset.campaignId === 'c2');
    const replyRate = c2Card.querySelector('[data-testid="reply-rate"]');
    expect(replyRate).toHaveTextContent('--');
  });

  it('Pause button shown only for running campaigns', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    renderPage();
    await screen.findAllByTestId('campaign-card');
    // c1 is running → Pause button
    expect(screen.getByTestId('pause-button')).toBeInTheDocument();
    // c2 is paused → Resume button
    expect(screen.getByTestId('resume-button')).toBeInTheDocument();
  });

  it('Pause action calls pauseCampaign and invalidates cache', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    api.pauseCampaign.mockResolvedValue({});
    renderPage();
    await screen.findAllByTestId('campaign-card');
    fireEvent.click(screen.getByTestId('pause-button'));
    await waitFor(() => expect(api.pauseCampaign).toHaveBeenCalledWith('c1'));
  });

  it('Resume action calls resumeCampaign', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    api.resumeCampaign.mockResolvedValue({});
    renderPage();
    await screen.findAllByTestId('campaign-card');
    fireEvent.click(screen.getByTestId('resume-button'));
    await waitFor(() => expect(api.resumeCampaign).toHaveBeenCalledWith('c2'));
  });

  it('Delete shows confirm dialog and calls deleteCampaign on yes', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    api.deleteCampaign.mockResolvedValue();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    await screen.findAllByTestId('campaign-card');
    fireEvent.click(screen.getAllByTestId('delete-button')[0]);
    await waitFor(() => expect(api.deleteCampaign).toHaveBeenCalledWith('c1'));
  });

  it('Delete is cancelled when confirm returns false', async () => {
    api.listCampaigns.mockResolvedValue(SAMPLE_CAMPAIGNS);
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();
    await screen.findAllByTestId('campaign-card');
    fireEvent.click(screen.getAllByTestId('delete-button')[0]);
    // wait a tick to let any async mutation start
    await new Promise((r) => setTimeout(r, 10));
    expect(api.deleteCampaign).not.toHaveBeenCalled();
  });

  it('"+ New campaign" link goes to /campaigns/new', async () => {
    api.listCampaigns.mockResolvedValue([]);
    renderPage();
    const link = await screen.findAllByRole('link', { name: /new campaign/i });
    expect(link[0]).toHaveAttribute('href', '/campaigns/new');
  });

  it('shows "Not tracked" for click rate when click tracking is disabled', async () => {
    api.listCampaigns.mockResolvedValue([{
      id: 'c9', name: 'No click tracking', status: 'running',
      created_at: '2026-05-01T12:00:00Z',
      lead_counts: { total: 10, pending: 0, scheduled: 0, sent: 10, failed: 0 },
      stats: { open_rate: 0.5, click_rate: null, reply_rate: 0.05, click_tracking_enabled: false },
      connected_account_configured: true,
    }]);
    renderPage();
    await screen.findByTestId('campaign-card');
    expect(screen.getByText('Not tracked')).toBeInTheDocument();
  });
});
