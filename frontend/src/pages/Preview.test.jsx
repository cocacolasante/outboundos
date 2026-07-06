import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Preview from './Preview.jsx';

vi.mock('../api/campaigns.js', () => ({
  getCampaign: vi.fn(),
  getPreview: vi.fn(),
  updateSample: vi.fn(),
  approveAll: vi.fn(),
  rejectPreview: vi.fn(),
}));

import * as api from '../api/campaigns.js';

const SAMPLES = [
  {
    lead_id: 'lead-1', email: 'a@x.com',
    first_name: 'Alice', last_name: 'Apple', company: 'Acme', job_title: 'CEO',
    research_quality: 'rich', research_summary: 'Series B',
    composed_subject: 'Hi 1', composed_body: 'Body 1',
    compose_status: 'done', sample_approved: null,
  },
  {
    lead_id: 'lead-2', email: 'b@x.com',
    first_name: 'Bob', last_name: 'Banana', company: 'Beeco', job_title: 'CTO',
    research_quality: 'low', research_summary: '',
    composed_subject: 'Hi 2', composed_body: 'Body 2',
    compose_status: 'done', sample_approved: true,
  },
];

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

function renderPreview() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/campaigns/abc-123/preview']}>
        <Routes>
          <Route path="/campaigns/:id/preview" element={<Preview />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  navigateMock.mockReset();
  api.getCampaign.mockResolvedValue({ id: 'abc-123', goal: 'Book demos', tone: 'Direct' });
  api.getPreview.mockResolvedValue({
    campaign_id: 'abc-123', status: 'previewing', samples: SAMPLES, all_ready: true,
  });
});

describe('Preview page', () => {
  it('renders campaign goal + tone context strip', async () => {
    renderPreview();
    const context = await screen.findByTestId('campaign-context');
    expect(context).toHaveTextContent(/book demos/i);
    expect(context).toHaveTextContent(/direct/i);
  });

  it('renders one card per sample', async () => {
    renderPreview();
    await waitFor(() => {
      expect(screen.getAllByTestId('email-preview-card')).toHaveLength(2);
    });
  });

  it('approval counter reflects approved samples', async () => {
    renderPreview();
    const counter = await screen.findByTestId('approval-counter');
    expect(counter).toHaveTextContent(/1.*of.*2.*approved/i);
  });

  it('approve-all button calls approveAll and navigates', async () => {
    api.approveAll.mockResolvedValue({});
    renderPreview();
    await screen.findByTestId('approval-counter');
    fireEvent.click(screen.getByRole('button', { name: /approve and launch/i }));
    await waitFor(() => expect(api.approveAll).toHaveBeenCalledWith('abc-123'));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/campaigns/abc-123'));
  });

  it('reject button calls rejectPreview and navigates', async () => {
    api.rejectPreview.mockResolvedValue({});
    renderPreview();
    await screen.findByTestId('approval-counter');
    fireEvent.click(screen.getByRole('button', { name: /reject and reconfigure/i }));
    await waitFor(() => expect(api.rejectPreview).toHaveBeenCalledWith('abc-123'));
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
  });

  it('approve button on a card calls updateSample', async () => {
    api.updateSample.mockResolvedValue({ ...SAMPLES[0], sample_approved: true });
    renderPreview();
    const cards = await screen.findAllByTestId('email-preview-card');
    const approveBtn = cards[0].querySelector('[data-testid="approve-button"]');
    fireEvent.click(approveBtn);
    await waitFor(() => {
      expect(api.updateSample).toHaveBeenCalledWith('abc-123', 'lead-1', { approved: true });
    });
  });

  it('empty samples renders placeholder', async () => {
    api.getPreview.mockResolvedValue({
      campaign_id: 'abc-123', status: 'previewing', samples: [], all_ready: false,
    });
    renderPreview();
    expect(await screen.findByText(/no samples available/i)).toBeInTheDocument();
  });
});
