import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/agent.js', () => ({
  listReplies: vi.fn(),
}));
vi.mock('../api/crm.js', () => ({
  convertLead: vi.fn(),
}));

import * as agentApi from '../api/agent.js';
import * as crmApi from '../api/crm.js';
import Replies from './Replies.jsx';
import { ToastProvider } from '../components/Toast.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter initialEntries={['/replies']}>
          <Replies />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const ITEM = {
  activity_id: 'a1',
  lead_id: 'l1',
  opportunity_id: null,
  sentiment: 'positive',
  subject: 'Re: Quick question',
  body_preview: 'Very interested — send pricing.',
  occurred_at: '2026-06-12T10:00:00Z',
  lead_email: 'jane@acme.io',
  lead_name: 'Jane Doe',
  lead_company: 'Acme',
  convert_eligible: true,
  converted: false,
  draft_body: null,
};

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 50, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  agentApi.listReplies.mockResolvedValue(paged([ITEM]));
});

describe('Replies page', () => {
  it('renders the triage row with sentiment badge and lead context', async () => {
    renderPage();
    const row = await screen.findByTestId('reply-row-a1');
    expect(within(row).getByTestId('sentiment-badge')).toHaveTextContent('positive');
    expect(within(row).getByText('Jane Doe')).toBeInTheDocument();
    expect(within(row).getByText(/Acme/)).toBeInTheDocument();
    expect(within(row).getByText(/send pricing/)).toBeInTheDocument();
  });

  it('Convert button calls convertLead and refreshes the feed', async () => {
    crmApi.convertLead.mockResolvedValue({ opportunity: { id: 'o1' } });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('convert-btn-a1'));
    await waitFor(() => {
      expect(crmApi.convertLead).toHaveBeenCalledWith('l1');
    });
  });

  it('converted lead shows the pill and no Convert button', async () => {
    agentApi.listReplies.mockResolvedValue(paged([{
      ...ITEM, converted: true, convert_eligible: false, opportunity_id: 'o1',
    }]));
    renderPage();
    const row = await screen.findByTestId('reply-row-a1');
    expect(within(row).getByTestId('converted-pill')).toBeInTheDocument();
    expect(within(row).queryByTestId('convert-btn-a1')).toBeNull();
  });

  it('View draft toggles the suggested-reply panel', async () => {
    agentApi.listReplies.mockResolvedValue(paged([{
      ...ITEM, draft_body: 'Hi Jane — happy to share pricing.',
    }]));
    const user = userEvent.setup();
    renderPage();
    expect(screen.queryByTestId('draft-panel-a1')).toBeNull();
    await user.click(await screen.findByTestId('view-draft-btn-a1'));
    const panel = screen.getByTestId('draft-panel-a1');
    expect(panel).toHaveTextContent('happy to share pricing');
    expect(panel).toHaveTextContent(/never sent automatically/i);
  });

  it('sentiment filter re-queries with the param', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('reply-row-a1');
    await user.selectOptions(screen.getByTestId('sentiment-filter'), 'negative');
    await waitFor(() => {
      expect(agentApi.listReplies).toHaveBeenCalledWith({ sentiment: 'negative' });
    });
  });

  it('empty feed renders the explainer', async () => {
    agentApi.listReplies.mockResolvedValue(paged([]));
    renderPage();
    expect(await screen.findByTestId('replies-empty')).toBeInTheDocument();
  });
});
