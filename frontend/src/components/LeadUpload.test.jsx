import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import LeadUpload from './LeadUpload.jsx';

vi.mock('../api/campaigns.js', () => ({
  uploadLeadsPreview: vi.fn(),
  confirmLeadsUpload: vi.fn(),
}));

import * as api from '../api/campaigns.js';

beforeEach(() => {
  vi.clearAllMocks();
});

function makeCsvFile(content = 'Email,First Name\na@x.com,Alice\n') {
  return new File([content], 'leads.csv', { type: 'text/csv' });
}

describe('LeadUpload', () => {
  it('shows file picker initially', () => {
    render(<LeadUpload campaignId="c1" onComplete={() => {}} />);
    expect(screen.getByText(/choose csv/i)).toBeInTheDocument();
  });

  it('uploading a file calls uploadLeadsPreview and renders mapping table', async () => {
    api.uploadLeadsPreview.mockResolvedValue({
      columns: ['Email', 'First Name'],
      preview_rows: [{ Email: 'a@x.com', 'First Name': 'Alice' }],
      suggested_mapping: { Email: 'email', 'First Name': 'first_name' },
      total_rows: 1,
    });
    render(<LeadUpload campaignId="c1" onComplete={() => {}} />);

    const file = makeCsvFile();
    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [file] } });

    await waitFor(() => expect(api.uploadLeadsPreview).toHaveBeenCalled());
    const [campaignId, uploadedFile] = api.uploadLeadsPreview.mock.calls[0];
    expect(campaignId).toBe('c1');
    expect(uploadedFile.name).toBe('leads.csv');

    expect(await screen.findByTestId('mapping-table')).toBeInTheDocument();
    expect(screen.getByText(/1 rows/i)).toBeInTheDocument();
    // Suggested mapping populates the selects
    expect(screen.getByLabelText('Map Email')).toHaveValue('email');
    expect(screen.getByLabelText('Map First Name')).toHaveValue('first_name');
  });

  it('confirm posts the user-adjusted mapping and calls onComplete', async () => {
    api.uploadLeadsPreview.mockResolvedValue({
      columns: ['Email', 'Notes'],
      preview_rows: [{ Email: 'a@x.com', Notes: 'whatever' }],
      suggested_mapping: { Email: 'email' },
      total_rows: 1,
    });
    api.confirmLeadsUpload.mockResolvedValue({
      total: 1, suppressed: 0, duplicates_removed: 0, samples_selected: 1,
    });
    const onComplete = vi.fn();
    render(<LeadUpload campaignId="c1" onComplete={onComplete} />);

    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [makeCsvFile()] } });
    await screen.findByTestId('mapping-table');

    // User adjusts mapping for Notes → company
    fireEvent.change(screen.getByLabelText('Map Notes'), { target: { value: 'company' } });

    fireEvent.click(screen.getByRole('button', { name: /import 1 leads/i }));

    await waitFor(() => expect(api.confirmLeadsUpload).toHaveBeenCalled());
    const [campaignId, file, mapping] = api.confirmLeadsUpload.mock.calls[0];
    expect(campaignId).toBe('c1');
    expect(file).toBeInstanceOf(File);
    expect(mapping).toEqual({ Email: 'email', Notes: 'company' });

    await waitFor(() => expect(onComplete).toHaveBeenCalled());
  });

  it('blocks confirm when no column maps to email', async () => {
    api.uploadLeadsPreview.mockResolvedValue({
      columns: ['Name'],
      preview_rows: [{ Name: 'Alice' }],
      suggested_mapping: {},
      total_rows: 1,
    });
    render(<LeadUpload campaignId="c1" onComplete={() => {}} />);
    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [makeCsvFile()] } });
    await screen.findByTestId('mapping-table');

    fireEvent.click(screen.getByRole('button', { name: /import/i }));
    expect(await screen.findByTestId('upload-error')).toHaveTextContent(/email/i);
    expect(api.confirmLeadsUpload).not.toHaveBeenCalled();
  });

  it('shows error from preview failure', async () => {
    api.uploadLeadsPreview.mockRejectedValue({
      response: { data: { detail: 'bad encoding' } },
    });
    render(<LeadUpload campaignId="c1" onComplete={() => {}} />);
    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [makeCsvFile()] } });
    expect(await screen.findByTestId('upload-error')).toHaveTextContent(/bad encoding/i);
  });
});
