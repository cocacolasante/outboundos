import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/crm.js', () => ({
  getOpportunity: vi.fn(),
  updateOpportunity: vi.fn(),
  deleteOpportunity: vi.fn(),
  listOpportunities: vi.fn(),
  createOpportunity: vi.fn(),
  getPipelineSummary: vi.fn(),
  listDocuments: vi.fn(),
  uploadDocument: vi.fn(),
  deleteDocument: vi.fn(),
  documentDownloadUrl: vi.fn((id) => `http://test/crm/documents/${id}/download`),
  listProducts: vi.fn(),
  addProduct: vi.fn(),
  updateProduct: vi.fn(),
  deleteProduct: vi.fn(),
  listActivities: vi.fn(),
  createActivity: vi.fn(),
  updateActivity: vi.fn(),
  deleteActivity: vi.fn(),
}));

import * as api from '../api/crm.js';
import OpportunityDetail from './OpportunityDetail.jsx';
import { ToastProvider } from '../components/Toast.jsx';

const OPP = {
  id: 'o1', name: 'Acme — managed IT', stage: 'proposal',
  amount: 25000, close_date: '2026-07-15', probability: 50,
  description: 'Big migration deal', closed_at: null, loss_reason: null,
  first_name: 'Jane', last_name: 'Doe', email: 'jane@acme.io',
  phone: '+1-555', company: 'Acme', job_title: 'CFO',
  linkedin_url: 'https://www.linkedin.com/in/jane/',
  source_lead_id: 'l1',
  created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z',
  activity_count: 0, open_task_count: 0,
};

function renderPage(oppId = 'o1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter initialEntries={[`/opportunities/${oppId}`]}>
          <Routes>
            <Route path="/opportunities/:id" element={<OpportunityDetail />} />
            <Route path="/opportunities" element={<div data-testid="pipeline-page" />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 50, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getOpportunity.mockResolvedValue(OPP);
  api.listDocuments.mockResolvedValue([]);
  api.listProducts.mockResolvedValue({ items: [], products_total: 0 });
  api.listActivities.mockResolvedValue(paged([]));
  api.documentDownloadUrl.mockImplementation((id) => `http://test/crm/documents/${id}/download`);
});

describe('OpportunityDetail page', () => {
  it('renders the deal header, amount, and all main sections', async () => {
    renderPage();
    expect(await screen.findByTestId('opp-name')).toHaveTextContent('Acme — managed IT');
    expect(screen.getByText('$25,000')).toBeInTheDocument();
    expect(screen.getByText(/50% probability/)).toBeInTheDocument();
    // All Salesforce-lite sections are on the page.
    expect(screen.getByTestId('stage-stepper')).toBeInTheDocument();
    expect(screen.getByTestId('details-card')).toBeInTheDocument();
    expect(screen.getByTestId('products-card')).toBeInTheDocument();
    expect(screen.getByTestId('documents-card')).toBeInTheDocument();
    expect(screen.getByTestId('activity-log')).toBeInTheDocument();
  });

  it('advancing the stage calls updateOpportunity', async () => {
    api.updateOpportunity.mockResolvedValue({ ...OPP, stage: 'negotiation' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('set-stage-negotiation'));
    await waitFor(() => {
      expect(api.updateOpportunity).toHaveBeenCalledWith('o1', { stage: 'negotiation' });
    });
  });

  it('closed_won asks for confirmation before marking won', async () => {
    api.updateOpportunity.mockResolvedValue({ ...OPP, stage: 'closed_won' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('set-stage-closed_won'));
    await waitFor(() => {
      expect(api.updateOpportunity).toHaveBeenCalledWith('o1', { stage: 'closed_won' });
    });
  });

  it('closed_lost requires a loss reason before the stage flips', async () => {
    api.updateOpportunity.mockResolvedValue({
      ...OPP, stage: 'closed_lost', loss_reason: 'budget cut',
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('set-stage-closed_lost'));
    // No API call yet — the reason form opens instead.
    expect(api.updateOpportunity).not.toHaveBeenCalled();
    const form = screen.getByTestId('loss-reason-form');
    // Mark-lost disabled until a reason is typed.
    expect(within(form).getByTestId('confirm-closed-lost')).toBeDisabled();

    await user.type(screen.getByTestId('loss-reason-input'), 'budget cut');
    await user.click(screen.getByTestId('confirm-closed-lost'));
    await waitFor(() => {
      expect(api.updateOpportunity).toHaveBeenCalledWith('o1', {
        stage: 'closed_lost', loss_reason: 'budget cut',
      });
    });
  });

  it('shows the won banner + loss reason display on closed deals', async () => {
    api.getOpportunity.mockResolvedValue({
      ...OPP, stage: 'closed_lost', closed_at: '2026-06-10T10:00:00Z',
      loss_reason: 'went with incumbent',
    });
    renderPage();
    expect(await screen.findByTestId('closed-banner')).toHaveTextContent('Lost');
    expect(screen.getByTestId('loss-reason-display')).toHaveTextContent('went with incumbent');
  });

  it('adds a product of interest and shows line totals', async () => {
    api.addProduct.mockResolvedValue({ id: 'p1' });
    api.listProducts.mockResolvedValue({
      items: [{
        id: 'p1', opportunity_id: 'o1', product_name: 'Managed IT (24 seats)',
        quantity: 24, unit_price: 95, notes: null, line_total: 2280,
        created_at: '2026-06-11T10:00:00Z',
      }],
      products_total: 2280,
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('add-product-toggle'));
    await user.type(screen.getByTestId('product-name-input'), 'Managed IT (24 seats)');
    await user.clear(screen.getByTestId('product-qty-input'));
    await user.type(screen.getByTestId('product-qty-input'), '24');
    await user.type(screen.getByTestId('product-price-input'), '95');
    await user.click(screen.getByTestId('product-save-btn'));

    await waitFor(() => {
      expect(api.addProduct).toHaveBeenCalledWith('o1', {
        product_name: 'Managed IT (24 seats)', quantity: 24, unit_price: 95,
      });
    });
    // Re-fetched list renders the row + the total.
    expect(await screen.findByTestId('product-row-p1')).toBeInTheDocument();
    expect(screen.getByTestId('products-total')).toHaveTextContent('$2,280');
  });

  it('uploads a document via the file input', async () => {
    api.uploadDocument.mockResolvedValue({ id: 'd1' });
    const user = userEvent.setup();
    renderPage();

    const input = await screen.findByTestId('document-file-input');
    const file = new File(['fake pdf'], 'proposal.pdf', { type: 'application/pdf' });
    await user.upload(input, file);

    await waitFor(() => {
      expect(api.uploadDocument).toHaveBeenCalledWith('o1', file);
    });
  });

  it('lists documents with download links', async () => {
    api.listDocuments.mockResolvedValue([{
      id: 'd1', opportunity_id: 'o1', filename: 'contract.pdf',
      content_type: 'application/pdf', size_bytes: 52428,
      uploaded_at: '2026-06-11T10:00:00Z',
    }]);
    renderPage();
    const row = await screen.findByTestId('document-row-d1');
    const link = within(row).getByTestId('document-download-d1');
    expect(link).toHaveTextContent('contract.pdf');
    expect(link).toHaveAttribute('href', 'http://test/crm/documents/d1/download');
    expect(within(row).getByText(/51 KB/)).toBeInTheDocument();
  });

  it('delete navigates back to the pipeline', async () => {
    api.deleteOpportunity.mockResolvedValue();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('delete-opportunity-btn'));
    await waitFor(() => expect(api.deleteOpportunity).toHaveBeenCalledWith('o1'));
    expect(await screen.findByTestId('pipeline-page')).toBeInTheDocument();
  });
});
