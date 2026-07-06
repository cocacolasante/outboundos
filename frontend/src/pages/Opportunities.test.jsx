import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/crm.js', () => ({
  listOpportunities: vi.fn(),
  getOpportunity: vi.fn(),
  createOpportunity: vi.fn(),
  updateOpportunity: vi.fn(),
  deleteOpportunity: vi.fn(),
  getPipelineSummary: vi.fn(),
  getDefaultPipeline: vi.fn(),
  getStageHistory: vi.fn(),
  listActivities: vi.fn(),
  createActivity: vi.fn(),
  updateActivity: vi.fn(),
  deleteActivity: vi.fn(),
}));

import * as api from '../api/crm.js';
import Opportunities, { resolveDropTarget, fmtAmount } from './Opportunities.jsx';
import { ToastProvider } from '../components/Toast.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter initialEntries={['/opportunities']}>
          <Routes>
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/opportunities/:id" element={<div data-testid="detail-page" />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const OPP = {
  id: 'o1', name: 'Acme — managed IT', stage: 'qualification',
  amount: 25000, close_date: '2026-07-15', probability: 25,
  description: null, closed_at: null, loss_reason: null,
  first_name: 'Jane', last_name: 'Doe', email: 'jane@acme.io',
  phone: null, company: 'Acme', job_title: 'CFO', linkedin_url: null,
  source_lead_id: 'l1', created_at: '2026-06-11T10:00:00Z',
  updated_at: '2026-06-11T10:00:00Z',
  activity_count: 1, open_task_count: 2,
};

const PIPELINE = {
  id: 'p1', name: 'Default', is_default: true,
  stages: [
    { id: 's0', key: 'prospecting', name: 'Prospecting', sort_order: 0, default_probability: 10, is_won: false, is_lost: false },
    { id: 's1', key: 'qualification', name: 'Qualification', sort_order: 1, default_probability: 25, is_won: false, is_lost: false },
    { id: 's2', key: 'proposal', name: 'Proposal', sort_order: 2, default_probability: 50, is_won: false, is_lost: false },
    { id: 's3', key: 'negotiation', name: 'Negotiation', sort_order: 3, default_probability: 75, is_won: false, is_lost: false },
    { id: 's4', key: 'closed_won', name: 'Closed Won', sort_order: 4, default_probability: 100, is_won: true, is_lost: false },
    { id: 's5', key: 'closed_lost', name: 'Closed Lost', sort_order: 5, default_probability: 0, is_won: false, is_lost: true },
  ],
};

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 500, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listOpportunities.mockResolvedValue(paged([OPP]));
  api.getDefaultPipeline.mockResolvedValue(PIPELINE);
  api.getPipelineSummary.mockResolvedValue([]);
  api.listActivities.mockResolvedValue(paged([]));
});

describe('resolveDropTarget', () => {
  const stageKeys = ['prospecting', 'qualification', 'proposal'];
  const cardStage = { o1: 'qualification', o2: 'proposal' };

  it('returns the stage key when dropped on a column', () => {
    expect(resolveDropTarget('proposal', stageKeys, cardStage)).toBe('proposal');
  });
  it('maps a card id to its current stage when dropped on a card', () => {
    expect(resolveDropTarget('o2', stageKeys, cardStage)).toBe('proposal');
  });
  it('returns null for unknown / missing targets', () => {
    expect(resolveDropTarget(null, stageKeys, cardStage)).toBeNull();
    expect(resolveDropTarget('nope', stageKeys, cardStage)).toBeNull();
  });
});

describe('fmtAmount', () => {
  it('formats currency and handles null', () => {
    expect(fmtAmount(25000)).toMatch(/\$25,000/);
    expect(fmtAmount(null)).toBe('—');
  });
});

describe('Opportunities board', () => {
  it('renders columns from the configurable pipeline with the card in its stage', async () => {
    renderPage();
    const col = await screen.findByTestId('stage-column-qualification');
    const card = within(col).getByTestId('opp-card-o1');
    expect(within(card).getByText('Acme — managed IT')).toBeInTheDocument();
    expect(within(card).getByText(/\$25,000/)).toBeInTheDocument();
    expect(within(card).getByText(/2 open tasks/)).toBeInTheDocument();
    // Per-column total computed from the cards (live under optimistic moves).
    expect(within(col).getByTestId('stage-total-qualification')).toHaveTextContent('1 · $25,000');
  });

  it('closed columns hidden by default; toggle shows them', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pipeline-board');
    expect(screen.queryByTestId('stage-column-closed_won')).toBeNull();
    await user.click(screen.getByTestId('show-closed-toggle'));
    expect(screen.getByTestId('stage-column-closed_won')).toBeInTheDocument();
  });

  it('cards + columns expose accessible labels for keyboard drag', async () => {
    renderPage();
    const card = await screen.findByTestId('opp-card-o1');
    // Card is focusable and self-describing for screen readers.
    expect(card).toHaveAttribute('tabindex', '0');
    expect(card.getAttribute('aria-label')).toMatch(/Acme — managed IT/);
    expect(card.getAttribute('aria-label')).toMatch(/pick up/i);
    // Droppable column is a labelled group.
    const col = screen.getByTestId('stage-column-qualification');
    expect(col).toHaveAttribute('role', 'group');
    expect(col.getAttribute('aria-label')).toMatch(/Qualification stage/);
  });

  it('clicking a card navigates to the opportunity detail page', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('opp-card-o1'));
    expect(await screen.findByTestId('detail-page')).toBeInTheDocument();
  });

  it('list view renders a table; row click navigates', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pipeline-board');
    await user.click(screen.getByTestId('view-list'));
    const table = await screen.findByTestId('opportunity-list');
    expect(within(table).getByText('Acme — managed IT')).toBeInTheDocument();
    expect(within(table).getByText('Qualification')).toBeInTheDocument();
    await user.click(screen.getByTestId('opp-row-o1'));
    expect(await screen.findByTestId('detail-page')).toBeInTheDocument();
  });

  it('falls back to the enum stages when the pipeline endpoint fails', async () => {
    api.getDefaultPipeline.mockRejectedValue(new Error('404'));
    renderPage();
    // Board still renders all non-closed columns from the STAGES fallback.
    expect(await screen.findByTestId('stage-column-qualification')).toBeInTheDocument();
    expect(screen.getByTestId('stage-column-prospecting')).toBeInTheDocument();
  });

  it('creates a new opportunity via the modal', async () => {
    api.createOpportunity.mockResolvedValue({ ...OPP, id: 'o2', name: 'Fresh deal' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-opportunity-btn'));
    await user.type(screen.getByTestId('new-opp-name'), 'Fresh deal');
    await user.type(screen.getByTestId('new-opp-amount'), '5000');
    await user.click(screen.getByTestId('new-opp-save'));
    await waitFor(() => {
      const payload = api.createOpportunity.mock.calls[0][0];
      expect(payload.name).toBe('Fresh deal');
      expect(payload.amount).toBe(5000);
    });
  });
});
