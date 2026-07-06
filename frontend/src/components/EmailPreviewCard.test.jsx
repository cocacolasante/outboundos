import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import EmailPreviewCard from './EmailPreviewCard.jsx';

const SAMPLE = {
  lead_id: 'lead-1',
  email: 'jane@acme.com',
  first_name: 'Jane',
  last_name: 'Doe',
  company: 'Acme',
  job_title: 'CEO',
  research_quality: 'rich',
  research_summary: 'Raised Series B; recent product launch.',
  composed_subject: 'Quick question',
  composed_body: 'Hi Jane,\n\nValue prop here.',
  compose_status: 'done',
  sample_approved: null,
};

function renderCard(overrides = {}) {
  const onSave = vi.fn().mockResolvedValue({ ...SAMPLE, ...overrides.sample });
  const onApprove = vi.fn().mockResolvedValue();
  const utils = render(
    <EmailPreviewCard
      sample={overrides.sample || SAMPLE}
      remainingSamples={overrides.remaining ?? 4}
      onSave={onSave}
      onApprove={onApprove}
    />
  );
  return { ...utils, onSave, onApprove };
}

describe('EmailPreviewCard', () => {
  it('renders lead identity and quality badge', () => {
    renderCard();
    expect(screen.getByRole('heading', { name: /jane doe/i })).toBeInTheDocument();
    expect(screen.getByText(/ceo at acme/i)).toBeInTheDocument();
    const badge = screen.getByTestId('quality-badge');
    expect(badge).toHaveAttribute('data-quality', 'rich');
    expect(badge).toHaveTextContent(/rich/i);
  });

  it('research panel is collapsed by default and toggles open', () => {
    renderCard();
    expect(screen.queryByTestId('research-panel')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /show research/i }));
    expect(screen.getByTestId('research-panel')).toHaveTextContent(/series b/i);
  });

  it('shows subject and body in editable inputs', () => {
    renderCard();
    expect(screen.getByLabelText(/subject/i)).toHaveValue('Quick question');
    expect(screen.getByLabelText(/body/i)).toHaveValue('Hi Jane,\n\nValue prop here.');
  });

  it('edits to body trigger save on blur', async () => {
    const { onSave } = renderCard();
    const body = screen.getByLabelText(/body/i);
    fireEvent.change(body, { target: { value: 'Hi Jane,\n\nRevised punchy body.' } });
    fireEvent.blur(body);
    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith('lead-1', {
        composed_body: 'Hi Jane,\n\nRevised punchy body.',
      });
    });
  });

  it('edits to subject trigger save on blur', async () => {
    const { onSave } = renderCard();
    const subj = screen.getByLabelText(/subject/i);
    fireEvent.change(subj, { target: { value: 'Better subject' } });
    fireEvent.blur(subj);
    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith('lead-1', { composed_subject: 'Better subject' });
    });
  });

  it('no-op blur (no edits) does not call onSave', async () => {
    const { onSave } = renderCard();
    fireEvent.blur(screen.getByLabelText(/subject/i));
    await new Promise((r) => setTimeout(r, 30));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('shows dirty hint when body edited', () => {
    renderCard({ remaining: 4 });
    fireEvent.change(screen.getByLabelText(/body/i), { target: { value: 'Different' } });
    expect(screen.getByTestId('dirty-hint')).toHaveTextContent(/improve the remaining 4/i);
  });

  it('Approve button calls onApprove with true', () => {
    const { onApprove } = renderCard();
    fireEvent.click(screen.getByTestId('approve-button'));
    expect(onApprove).toHaveBeenCalledWith('lead-1', true);
  });

  it('Reject button calls onApprove with false', () => {
    const { onApprove } = renderCard();
    fireEvent.click(screen.getByTestId('reject-button'));
    expect(onApprove).toHaveBeenCalledWith('lead-1', false);
  });

  it('Approve button shows pressed when sample_approved=true', () => {
    renderCard({ sample: { ...SAMPLE, sample_approved: true } });
    expect(screen.getByTestId('approve-button')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('reject-button')).toHaveAttribute('aria-pressed', 'false');
  });
});
