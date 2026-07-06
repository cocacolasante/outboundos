import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/researchClient.js', () => ({
  researchClient: vi.fn(),
  sendClientEmail: vi.fn(),
}));

// listAccounts powers the Send-from picker.  Default empty so existing
// tests don't see the dropdown unless they opt in.
vi.mock('../api/connectedAccounts.js', () => ({
  listAccounts: vi.fn().mockResolvedValue([]),
}));

import * as api from '../api/researchClient.js';
import * as accountsApi from '../api/connectedAccounts.js';
import ResearchClient from './ResearchClient.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ResearchClient />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // Restore the default-empty connected-accounts response; tests that
  // need a populated list override this explicitly.
  accountsApi.listAccounts.mockResolvedValue([]);
  // Avoid bleed from a prior test.
  try { localStorage.removeItem('researchClient.senderName'); } catch { /* noop */ }
});


describe('ResearchClient page', () => {
  it('renders form fields with sensible defaults', () => {
    renderPage();
    expect(screen.getByTestId('input-linkedin-url')).toBeInTheDocument();
    expect(screen.getByTestId('input-goal')).toBeInTheDocument();
    // DM is the default output kind so the char limit should default to 300.
    expect(screen.getByTestId('input-char-limit')).toHaveValue(300);
    // Default depth is fast.
    expect(screen.getByTestId('mode-fast')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('mode-deep')).toHaveAttribute('aria-pressed', 'false');
  });

  it('swaps the default char limit when output kind toggles to email', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('output-email'));
    expect(screen.getByTestId('input-char-limit')).toHaveValue(600);
    await user.click(screen.getByTestId('output-dm'));
    expect(screen.getByTestId('input-char-limit')).toHaveValue(300);
  });

  it('preserves a user-supplied char limit when output kind toggles', async () => {
    const user = userEvent.setup();
    renderPage();
    const charInput = screen.getByTestId('input-char-limit');
    await user.clear(charInput);
    await user.type(charInput, '450');
    await user.click(screen.getByTestId('output-email'));
    // User-edited value should NOT be overwritten by the email default.
    expect(charInput).toHaveValue(450);
  });

  it('submit is disabled until url + goal are filled', async () => {
    const user = userEvent.setup();
    renderPage();
    const btn = screen.getByTestId('submit-research');
    expect(btn).toBeDisabled();
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/jane/');
    expect(btn).toBeDisabled();
    await user.type(screen.getByTestId('input-goal'), 'Book a call');
    expect(btn).toBeEnabled();
  });

  it('submits the form and renders the composed message', async () => {
    const user = userEvent.setup();
    api.researchClient.mockResolvedValue({
      profile: {
        first_name: 'Jane', last_name: 'Doe',
        headline: 'Founder', company: 'Acme',
        company_website: 'acme.io', job_title: 'CEO',
        industry: 'SaaS', found: true, quality: 'rich',
      },
      research: {
        person_news: ['raised Series B'],
        company_news: ['shipped Acme Pro'],
        company_description: 'B2B platform',
        recent_updates: [], industry: 'SaaS',
      },
      subject: 'Congrats on the raise',
      body: 'Hi Jane, congrats on the raise.',
      char_count: 30,
      duration_ms: 8200,
    });

    renderPage();
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/jane-doe/');
    await user.type(screen.getByTestId('input-goal'), 'Book a discovery call');
    await user.click(screen.getByTestId('submit-research'));

    await waitFor(() => {
      expect(api.researchClient).toHaveBeenCalledTimes(1);
    });
    expect(api.researchClient.mock.calls[0][0]).toMatchObject({
      linkedin_url: 'https://www.linkedin.com/in/jane-doe/',
      goal: 'Book a discovery call',
      output_kind: 'linkedin_dm',
      research_mode: 'fast',
      char_limit: 300,
    });

    await waitFor(() => {
      expect(screen.getByTestId('result-panel')).toBeInTheDocument();
    });
    expect(screen.getByTestId('result-body')).toHaveValue('Hi Jane, congrats on the raise.');
    // DM also surfaces a subject (thread topic) — same UI as email.
    expect(screen.getByTestId('result-subject')).toHaveValue('Congrats on the raise');
    expect(screen.getByTestId('char-count')).toHaveTextContent('30 / 300');
  });

  it('hides the subject input when the server returns an empty subject', async () => {
    const user = userEvent.setup();
    api.researchClient.mockResolvedValue({
      profile: {
        first_name: 'X', last_name: 'Y', headline: '',
        company: '', company_website: '', job_title: '',
        industry: '', found: false, quality: 'low',
      },
      research: { person_news: [], company_news: [], company_description: '' },
      subject: '',  // model couldn't produce one; UI should silently omit
      body: 'Hi.',
      char_count: 3,
      duration_ms: 1000,
    });

    renderPage();
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/x/');
    await user.type(screen.getByTestId('input-goal'), 'ping');
    await user.click(screen.getByTestId('submit-research'));

    await waitFor(() => {
      expect(screen.getByTestId('result-panel')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('result-subject')).not.toBeInTheDocument();
  });

  it('shows the email subject on the email output path', async () => {
    const user = userEvent.setup();
    api.researchClient.mockResolvedValue({
      profile: {
        first_name: 'Jane', last_name: '', headline: '',
        company: 'Acme', company_website: '', job_title: '',
        industry: '', found: true, quality: 'partial',
      },
      research: { person_news: [], company_news: [], company_description: '' },
      subject: 'Quick thought',
      body: 'Hi Jane.',
      char_count: 8,
      duration_ms: 1000,
    });

    renderPage();
    await user.click(screen.getByTestId('output-email'));
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/jane/');
    await user.type(screen.getByTestId('input-goal'), 'Book a call');
    await user.click(screen.getByTestId('submit-research'));

    await waitFor(() => {
      expect(screen.getByTestId('result-subject')).toHaveValue('Quick thought');
    });
  });

  it('surfaces backend error detail', async () => {
    const user = userEvent.setup();
    api.researchClient.mockRejectedValue({
      response: { data: { detail: 'URL must be a LinkedIn profile URL' } },
      message: 'Request failed with status code 400',
    });

    renderPage();
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://example.com/');
    await user.type(screen.getByTestId('input-goal'), 'ping');
    await user.click(screen.getByTestId('submit-research'));

    await waitFor(() => {
      expect(screen.getByTestId('error-message')).toHaveTextContent(/LinkedIn profile URL/);
    });
  });
});


// ----------------------------------------------------------------------------
// Email Add / Edit / Send action panel
// ----------------------------------------------------------------------------

const EMAIL_RESULT_FIXTURE = {
  profile: {
    first_name: 'Jane',
    last_name: 'Doe',
    headline: 'Founder',
    company: 'Acme',
    company_website: 'acme.io',
    job_title: 'CEO',
    industry: 'SaaS',
    found: true,
    quality: 'rich',
  },
  research: {
    person_news: ['raised Series B (Mar 2026)'],
    company_news: [],
    company_description: 'B2B platform',
  },
  subject: 'Quick thought after your Series B',
  body: 'Hi Jane, congrats on the Series B raise.',
  char_count: 41,
  duration_ms: 800,
};

async function _composeEmail(user) {
  // Walk through the form → render the email result → return.  Shared
  // setup for the action-panel tests.
  api.researchClient.mockResolvedValue(EMAIL_RESULT_FIXTURE);
  renderPage();
  await user.click(screen.getByTestId('output-email'));
  await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/jane/');
  await user.type(screen.getByTestId('input-goal'), 'Book a call');
  await user.type(screen.getByTestId('input-sender-name'), 'Anthony');
  await user.click(screen.getByTestId('submit-research'));
  // Wait for the result panel + the Add email button to render.
  await waitFor(() => expect(screen.getByTestId('add-email-btn')).toBeInTheDocument());
}


describe('ResearchClient — email action panel', () => {
  it('Add email opens an editable form pre-filled with the composed subject + body', async () => {
    const user = userEvent.setup();
    await _composeEmail(user);

    // Idle state shows the Add email button, NOT the form.
    expect(screen.queryByTestId('email-action-form')).toBeNull();

    await user.click(screen.getByTestId('add-email-btn'));

    const form = await screen.findByTestId('email-action-form');
    expect(form).toBeInTheDocument();
    // Subject + body pre-filled and editable.
    expect(screen.getByTestId('send-subject')).toHaveValue('Quick thought after your Series B');
    expect(screen.getByTestId('send-body')).toHaveValue('Hi Jane, congrats on the Series B raise.');
    // Send button disabled until a valid email is entered.
    expect(screen.getByTestId('send-email-btn')).toBeDisabled();
  });

  it('typing a valid email enables Send; typing junk disables it', async () => {
    const user = userEvent.setup();
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    const emailInput = screen.getByTestId('send-to-email');
    const sendBtn = screen.getByTestId('send-email-btn');

    await user.type(emailInput, 'not-an-email');
    expect(sendBtn).toBeDisabled();

    await user.clear(emailInput);
    await user.type(emailInput, 'jane@example.com');
    expect(sendBtn).toBeEnabled();
  });

  it('edits to subject + body land in the API payload', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-1', sent_at: new Date().toISOString(), to_email: 'jane@example.com',
    });

    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    // Edit the subject.
    const subj = screen.getByTestId('send-subject');
    await user.clear(subj);
    await user.type(subj, 'Custom subject');

    // Edit the body (append).
    const bodyEl = screen.getByTestId('send-body');
    await user.click(bodyEl);
    await user.keyboard('{End} Looking forward!');

    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(api.sendClientEmail).toHaveBeenCalledTimes(1);
      const payload = api.sendClientEmail.mock.calls[0][0];
      expect(payload.to_email).toBe('jane@example.com');
      expect(payload.subject).toBe('Custom subject');
      expect(payload.body).toContain('Looking forward!');
      expect(payload.sender_name).toBe('Anthony');
      // to_name derived from the research profile.
      expect(payload.to_name).toBe('Jane Doe');
    });
  });

  it('successful send replaces the form with a confirmation row', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-1',
      sent_at: '2026-06-04T15:30:00Z',
      to_email: 'jane@example.com',
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('email-send-success')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('email-action-form')).toBeNull();
    expect(screen.getByTestId('email-send-success').textContent)
      .toMatch(/jane@example\.com/);
  });

  it('confirmation row shows the new-lead CRM note when crm_lead_created', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-1', sent_at: '2026-06-04T15:30:00Z',
      to_email: 'jane@example.com',
      crm_lead_id: 'l1', crm_lead_created: true, crm_activity_logged: true,
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('crm-tracking-note').textContent)
        .toMatch(/new lead/i);
    });
  });

  it('confirmation row shows the existing-record CRM note when the lead already existed', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-1', sent_at: '2026-06-04T15:30:00Z',
      to_email: 'jane@example.com',
      crm_lead_id: 'l1', crm_lead_created: false, crm_activity_logged: true,
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('crm-tracking-note').textContent)
        .toMatch(/existing CRM record/i);
    });
  });

  it('no CRM note when crm tracking failed server-side', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-1', sent_at: '2026-06-04T15:30:00Z',
      to_email: 'jane@example.com',
      crm_lead_id: null, crm_lead_created: false, crm_activity_logged: false,
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('email-send-success')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('crm-tracking-note')).toBeNull();
  });

  it('a backend 502 surfaces the detail inside the form (no toast needed)', async () => {
    const user = userEvent.setup();
    api.sendClientEmail.mockRejectedValue({
      response: { data: { detail: 'Brevo rejected the send (HTTP 401).' } },
      message: 'Request failed with status code 502',
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('send-error')).toHaveTextContent(/Brevo rejected/);
    });
    // Form stays open so the user can adjust + retry.
    expect(screen.getByTestId('email-action-form')).toBeInTheDocument();
  });

  it('Cancel returns to the idle state without sending', async () => {
    const user = userEvent.setup();
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    expect(screen.getByTestId('email-action-form')).toBeInTheDocument();

    // The Cancel button is the second of the two action buttons.
    const cancel = screen.getByRole('button', { name: /Cancel/i });
    await user.click(cancel);

    expect(screen.queryByTestId('email-action-form')).toBeNull();
    expect(screen.getByTestId('add-email-btn')).toBeInTheDocument();
    expect(api.sendClientEmail).not.toHaveBeenCalled();
  });

  it('Send-from picker is hidden when no connected accounts exist', async () => {
    const user = userEvent.setup();
    // The default beforeEach already mocks empty; explicit for clarity.
    accountsApi.listAccounts.mockResolvedValue([]);
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    // No dropdown rendered.
    expect(screen.queryByTestId('send-from-picker')).toBeNull();
  });

  it('Send-from picker lists every connected account + a default option', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'Anthony work', email_address: 'a@biz.com' },
      { id: 'a2', label: null, email_address: 'me@brand.com' },
    ]);
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    const picker = await screen.findByTestId('send-from-picker');
    // Default option ("Use configured default sender") + 2 accounts = 3 options.
    expect(picker.querySelectorAll('option')).toHaveLength(3);
    expect(picker).toHaveValue('');  // default option preselected
    // Account label format: "<label> — <email>"
    expect(picker.textContent).toMatch(/Anthony work — a@biz\.com/);
    // No-label account falls back to just the email.
    expect(picker.textContent).toMatch(/me@brand\.com/);
  });

  it('Picking a connected account sends its email_address as sender_email', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'Brand', email_address: 'hello@brand.com' },
    ]);
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-2', sent_at: new Date().toISOString(), to_email: 'jane@example.com',
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    const picker = await screen.findByTestId('send-from-picker');
    await user.selectOptions(picker, 'hello@brand.com');
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      const payload = api.sendClientEmail.mock.calls[0][0];
      expect(payload.sender_email).toBe('hello@brand.com');
    });
  });

  it('Leaving the picker on the default option omits sender_email from the payload', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'X', email_address: 'x@example.com' },
    ]);
    api.sendClientEmail.mockResolvedValue({
      message_id: 'msg-3', sent_at: new Date().toISOString(), to_email: 'jane@example.com',
    });
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    // Do NOT touch the picker — stays on the default.
    await user.type(screen.getByTestId('send-to-email'), 'jane@example.com');
    await user.click(screen.getByTestId('send-email-btn'));

    await waitFor(() => {
      const payload = api.sendClientEmail.mock.calls[0][0];
      expect(payload).not.toHaveProperty('sender_email');
    });
  });

  it('Signature preview renders the workspace default sender signature on the default option', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'Brand', email_address: 'brand@me.com',
        is_default_sender: true, signature: 'Best,\nAnthony\ncsuitecode.com' },
      { id: 'a2', label: 'Personal', email_address: 'me@gmail.com',
        is_default_sender: false, signature: '— Anthony' },
    ]);
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    // Picker is on "Use configured default sender" — the default
    // sender's signature ("Brand" / "Anthony / csuitecode.com") should
    // be the preview.
    const preview = await screen.findByTestId('signature-preview-text');
    expect(preview.textContent).toContain('csuitecode.com');
    expect(preview.textContent).toContain('Anthony');
  });

  it('Signature preview swaps when the user picks a different from-address', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'Brand', email_address: 'brand@me.com',
        is_default_sender: true, signature: 'Brand sig — csuitecode.com' },
      { id: 'a2', label: 'Personal', email_address: 'me@gmail.com',
        is_default_sender: false, signature: 'Personal sig — me@gmail.com' },
    ]);
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));

    // Default picker → brand signature.
    const preview = await screen.findByTestId('signature-preview-text');
    expect(preview.textContent).toContain('Brand sig');

    // Pick the personal account → preview swaps.
    await user.selectOptions(screen.getByTestId('send-from-picker'), 'me@gmail.com');
    await waitFor(() => {
      expect(screen.getByTestId('signature-preview-text').textContent)
        .toContain('Personal sig');
    });
  });

  it('Signature preview is hidden when the resolved account has no signature', async () => {
    const user = userEvent.setup();
    accountsApi.listAccounts.mockResolvedValue([
      { id: 'a1', label: 'Plain', email_address: 'plain@me.com',
        is_default_sender: true, signature: null },
    ]);
    await _composeEmail(user);
    await user.click(screen.getByTestId('add-email-btn'));
    // No signature preview surface at all.
    expect(screen.queryByTestId('signature-preview')).toBeNull();
  });

  it('LinkedIn DM output does NOT show the email send panel', async () => {
    const user = userEvent.setup();
    api.researchClient.mockResolvedValue({
      ...EMAIL_RESULT_FIXTURE,
      subject: '',  // DM mode has no subject
      body: 'short DM body',
    });
    renderPage();
    // Stay on the default DM output kind.
    await user.type(screen.getByTestId('input-linkedin-url'), 'https://www.linkedin.com/in/jane/');
    await user.type(screen.getByTestId('input-goal'), 'connect');
    await user.click(screen.getByTestId('submit-research'));

    await waitFor(() => {
      expect(screen.getByTestId('result-body')).toHaveValue('short DM body');
    });
    // The send-by-email panel is email-only.
    expect(screen.queryByTestId('add-email-btn')).toBeNull();
  });
});
