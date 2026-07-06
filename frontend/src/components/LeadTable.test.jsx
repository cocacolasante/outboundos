import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import LeadTable from './LeadTable.jsx';

vi.mock('../api/campaigns.js', () => ({
  listCampaignLeads: vi.fn(),
  deleteCampaignLead: vi.fn(),
}));

import * as api from '../api/campaigns.js';

const LEADS_PAGE = {
  items: [
    { id: 'l1', email: 'a@x.com', first_name: 'Alice', last_name: 'Apple', company: 'Acme', send_status: 'sent', created_at: '2026-05-12T10:00:00Z', sequence_status: 'active', sequence_stage: 'Email reply' },
    { id: 'l2', email: 'b@x.com', first_name: 'Bob', last_name: 'Banana', company: 'Beeco', send_status: 'pending', created_at: '2026-05-12T11:00:00Z', sequence_status: 'completed', sequence_stage: 'Completed' },
  ],
  total: 2,
  page: 1,
  page_size: 50,
  total_pages: 1,
};

function renderTable(props = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LeadTable campaignId="c1" replyTrackingEnabled={false} {...props} />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listCampaignLeads.mockResolvedValue(LEADS_PAGE);
});

describe('LeadTable', () => {
  it('renders rows from API', async () => {
    renderTable();
    await waitFor(() => {
      expect(screen.getByText('Alice Apple')).toBeInTheDocument();
      expect(screen.getByText('Bob Banana')).toBeInTheDocument();
    });
  });

  it('shows each lead\'s sequence stage', async () => {
    renderTable();
    await screen.findByText('Alice Apple');
    const stages = screen.getAllByTestId('row-sequence-stage');
    expect(stages.map((s) => s.textContent)).toEqual(['Email reply', 'Completed']);
  });

  it('search input refetches with search param', async () => {
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.change(screen.getByLabelText(/search leads/i), { target: { value: 'alice' } });

    await waitFor(() => {
      const lastCall = api.listCampaignLeads.mock.calls.at(-1);
      expect(lastCall[1]).toMatchObject({ search: 'alice', page: 1 });
    });
  });

  it('status filter refetches with send_status param', async () => {
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.change(screen.getByLabelText(/filter by send status/i), { target: { value: 'sent' } });

    await waitFor(() => {
      const lastCall = api.listCampaignLeads.mock.calls.at(-1);
      expect(lastCall[1]).toMatchObject({ send_status: 'sent' });
    });
  });

  it('shows pagination controls when total_pages > 1', async () => {
    api.listCampaignLeads.mockResolvedValue({ ...LEADS_PAGE, total: 100, total_pages: 2 });
    renderTable();
    await screen.findByRole('button', { name: /next page/i });
    expect(screen.getByText(/page 1 of 2/i)).toBeInTheDocument();
  });

  it('Next page increments page param', async () => {
    api.listCampaignLeads.mockResolvedValue({ ...LEADS_PAGE, total: 100, total_pages: 2 });
    renderTable();
    await screen.findByRole('button', { name: /next page/i });
    fireEvent.click(screen.getByRole('button', { name: /next page/i }));
    await waitFor(() => {
      const lastCall = api.listCampaignLeads.mock.calls.at(-1);
      expect(lastCall[1]).toMatchObject({ page: 2 });
    });
  });

  it('Export CSV creates a download', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:mock');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    renderTable();
    await screen.findByText('Alice Apple');
    fireEvent.click(screen.getByRole('button', { name: /export csv/i }));

    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('empty result shows "No leads" placeholder', async () => {
    api.listCampaignLeads.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50, total_pages: 0 });
    renderTable();
    expect(await screen.findByText(/no leads/i)).toBeInTheDocument();
  });

  it('Delete button opens a confirmation modal', async () => {
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.click(screen.getByTestId('delete-lead-l1'));
    const modal = await screen.findByTestId('delete-lead-modal');
    // Modal body explains the cascade so the user knows what they're agreeing to.
    expect(screen.getByText(/halts every future step/i)).toBeInTheDocument();
    // Scope the email lookup to the modal — the lead's email also appears in
    // the table row.
    expect(modal.textContent).toContain('a@x.com');
  });

  it('Confirm Delete fires deleteCampaignLead and closes the modal', async () => {
    api.deleteCampaignLead.mockResolvedValue(undefined);
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.click(screen.getByTestId('delete-lead-l1'));
    await screen.findByTestId('delete-lead-modal');
    fireEvent.click(screen.getByTestId('confirm-delete-lead'));

    await waitFor(() => {
      expect(api.deleteCampaignLead).toHaveBeenCalledWith('c1', 'l1');
    });
    // Modal closes on success.
    await waitFor(() => {
      expect(screen.queryByTestId('delete-lead-modal')).not.toBeInTheDocument();
    });
  });

  it('Cancel closes the modal without firing the delete', async () => {
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.click(screen.getByTestId('delete-lead-l1'));
    await screen.findByTestId('delete-lead-modal');
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(screen.queryByTestId('delete-lead-modal')).not.toBeInTheDocument();
    expect(api.deleteCampaignLead).not.toHaveBeenCalled();
  });

  it('shows backend error detail in the modal when delete fails', async () => {
    api.deleteCampaignLead.mockRejectedValue({
      response: { data: { detail: 'Lead not found' } },
      message: 'Request failed with status code 404',
    });
    renderTable();
    await screen.findByText('Alice Apple');

    fireEvent.click(screen.getByTestId('delete-lead-l1'));
    await screen.findByTestId('delete-lead-modal');
    fireEvent.click(screen.getByTestId('confirm-delete-lead'));

    await waitFor(() => {
      expect(screen.getByText(/lead not found/i)).toBeInTheDocument();
    });
  });
});
