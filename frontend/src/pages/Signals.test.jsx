import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/signals.js', () => ({
  listWatches: vi.fn(),
  createWatch: vi.fn(),
  createWatchesBulk: vi.fn(),
  updateWatch: vi.fn(),
  deleteWatch: vi.fn(),
  runWatchNow: vi.fn(),
  listSignals: vi.fn(),
  actionSignal: vi.fn(),
  dismissSignal: vi.fn(),
  draftSignalEmail: vi.fn(),
  sendSignalEmail: vi.fn(),
  addSignalsToCampaign: vi.fn(),
  enrichSignalContact: vi.fn(),
}));
vi.mock('../api/connectedAccounts.js', () => ({
  listAccounts: vi.fn(),
}));
vi.mock('../api/campaigns.js', () => ({
  listCampaigns: vi.fn(),
}));
vi.mock('../api/crm.js', () => ({
  convertLead: vi.fn(),
}));
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig()),
  useNavigate: () => mockNavigate,
}));

import * as api from '../api/signals.js';
import * as accountsApi from '../api/connectedAccounts.js';
import * as campaignsApi from '../api/campaigns.js';
import * as crmApi from '../api/crm.js';
import Signals from './Signals.jsx';
import { ToastProvider } from '../components/Toast.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter initialEntries={['/signals']}>
          <Signals />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const SIGNAL = {
  id: 's1', watch_id: 'w1', signal_type: 'job_change',
  summary: 'Jane Doe changed roles: Director of IT → VP of Engineering',
  detail: { old_title: 'Director of IT', new_title: 'VP of Engineering' },
  status: 'new', lead_id: 'l1', lead_has_email: true, opportunity_id: null,
  detected_at: '2026-06-12T10:00:00Z',
};

function paged(items) {
  return { items, total: items.length, page: 1, page_size: 50, total_pages: 1 };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listSignals.mockResolvedValue(paged([SIGNAL]));
  api.listWatches.mockResolvedValue([]);
  accountsApi.listAccounts.mockResolvedValue([
    { id: 'a1', label: 'Outreach', email_address: 'me@csuitecode.com', is_default_sender: true, signature: 'Anthony' },
  ]);
  campaignsApi.listCampaigns.mockResolvedValue([
    { id: 'c1', name: 'Nonprofit Q3', status: 'running' },
    { id: 'c2', name: 'Old one', status: 'complete' },
  ]);
});

describe('Signals page', () => {
  it('renders the feed with type badge and summary', async () => {
    renderPage();
    const card = await screen.findByTestId('signal-card-s1');
    expect(within(card).getByText('Job change')).toBeInTheDocument();
    expect(card).toHaveTextContent('VP of Engineering');
  });

  it('action + dismiss buttons call the API', async () => {
    api.actionSignal.mockResolvedValue({ id: 's1', status: 'actioned' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('action-signal-s1'));
    await waitFor(() => {
      expect(api.actionSignal).toHaveBeenCalled();
      expect(api.actionSignal.mock.calls[0][0]).toBe('s1');
    });
  });

  it('creates a cold-target watch from the Watches tab', async () => {
    api.createWatch.mockResolvedValue({ id: 'w2' });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Watches' }));
    await user.click(await screen.findByTestId('new-watch-btn'));
    await user.click(screen.getByTestId('watch-type-hiring'));
    await user.type(screen.getByTestId('watch-company'), 'Acme');
    await user.click(screen.getByTestId('save-watch-btn'));
    await waitFor(() => {
      expect(api.createWatch).toHaveBeenCalled();
      const payload = api.createWatch.mock.calls[0][0];
      expect(payload.watch_type).toBe('hiring');
      expect(payload.company).toBe('Acme');
    });
  });

  it('save disabled without the per-type required field', async () => {
    api.listSignals.mockResolvedValue(paged([]));
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByTestId('signals-empty')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Watches' }));
    await user.click(await screen.findByTestId('new-watch-btn'));

    // Default type (funding) requires a company.
    expect(screen.getByTestId('watch-type-desc')).toHaveTextContent(/COMPANY/);
    expect(screen.getByTestId('save-watch-btn')).toBeDisabled();

    // Job change requires an email — company field alone doesn't enable.
    await user.click(screen.getByTestId('watch-type-job_change'));
    expect(screen.getByTestId('watch-type-desc')).toHaveTextContent(/EMAIL/);
    expect(screen.getByTestId('save-watch-btn')).toBeDisabled();
    await user.type(screen.getByTestId('watch-email'), 'jane@acme.com');
    expect(screen.getByTestId('save-watch-btn')).toBeEnabled();
  });

  it('bulk mode creates one watch per pasted company', async () => {
    api.createWatchesBulk.mockResolvedValue({
      created: 3, skipped_duplicate: 1, watch_ids: ['a', 'b', 'c'],
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Watches' }));
    await user.click(await screen.findByTestId('new-watch-btn'));
    // funding (default) supports bulk; job-change does NOT show the toggle.
    await user.click(screen.getByTestId('watch-type-job_change'));
    expect(screen.queryByTestId('bulk-mode-toggle')).toBeNull();
    await user.click(screen.getByTestId('watch-type-funding'));

    await user.click(screen.getByTestId('bulk-mode-toggle'));
    await user.type(
      screen.getByTestId('bulk-companies-input'),
      'Acme Corp{enter}Beta Inc{enter}Gamma LLC',
    );
    await user.click(screen.getByTestId('save-watch-btn'));
    await waitFor(() => {
      expect(api.createWatchesBulk).toHaveBeenCalled();
      const payload = api.createWatchesBulk.mock.calls[0][0];
      expect(payload.watch_type).toBe('funding');
      expect(payload.companies).toEqual(['Acme Corp', 'Beta Inc', 'Gamma LLC']);
    });
  });

  it('renders the how-it-works explainer', async () => {
    renderPage();
    const help = await screen.findByTestId('signals-help');
    expect(help).toHaveTextContent(/How signals work/);
    expect(help).toHaveTextContent(/one.*company or person/i);
  });

  it('shows a source badge — Watch when no feed source', async () => {
    renderPage();
    const card = await screen.findByTestId('signal-card-s1');
    expect(within(card).getByTestId('signal-source-badge')).toHaveTextContent('Watch');
  });

  it('shows the feed source badge for discovery signals', async () => {
    api.listSignals.mockResolvedValue(paged([{
      ...SIGNAL, id: 's2', source: 'usaspending',
      signal_type: 'grant_awarded',
      summary: 'Helping Hands won a federal grant ($50,000)',
      watch_id: null,
    }]));
    renderPage();
    const card = await screen.findByTestId('signal-card-s2');
    expect(within(card).getByTestId('signal-source-badge')).toHaveTextContent('USASpending');
  });

  it('source filter passes the source param to the API', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('signal-card-s1');
    await user.selectOptions(screen.getByTestId('signal-source-filter'), 'irs_bmf');
    await waitFor(() => {
      const lastCall = api.listSignals.mock.calls.at(-1)[0];
      expect(lastCall.source).toBe('irs_bmf');
    });
  });
});

describe('Signal detail → draft → send', () => {
  it('clicking a card opens the detail modal', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    expect(await screen.findByTestId('signal-detail-modal')).toBeInTheDocument();
  });

  it('draft → send: composes, picks sender, sends, logs', async () => {
    api.draftSignalEmail.mockResolvedValue({
      to_email: 'jane@acme.com', to_name: 'Jane Doe',
      subject: 'Congrats on the new role', body: 'Hi Jane, quick call?',
    });
    api.sendSignalEmail.mockResolvedValue({
      message_id: 'm1', crm_lead_id: 'l1', crm_lead_created: false,
      crm_activity_logged: true, signal_status: 'actioned',
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('draft-signal-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');

    await user.click(within(modal).getByTestId('signal-draft-btn'));
    await waitFor(() => {
      expect(api.draftSignalEmail).toHaveBeenCalledWith('s1', {});
    });
    // Draft prefilled the editor.
    expect(within(modal).getByTestId('signal-subject')).toHaveValue('Congrats on the new role');
    expect(within(modal).getByTestId('signal-to')).toHaveTextContent('jane@acme.com');

    // Pick a sender, then send.
    await user.selectOptions(within(modal).getByTestId('signal-from-picker'), 'me@csuitecode.com');
    await user.click(within(modal).getByTestId('signal-send-btn'));
    await waitFor(() => {
      expect(api.sendSignalEmail).toHaveBeenCalled();
      const [id, payload] = api.sendSignalEmail.mock.calls[0];
      expect(id).toBe('s1');
      expect(payload.sender_email).toBe('me@csuitecode.com');
      expect(payload.subject).toBe('Congrats on the new role');
    });
    // Success + CRM note.
    expect(await screen.findByTestId('signal-send-success')).toBeInTheDocument();
    expect(screen.getByTestId('signal-crm-note')).toHaveTextContent(/outbound email activity/i);
  });

  it('shows notification-only note when the signal has no lead', async () => {
    api.listSignals.mockResolvedValue(paged([{ ...SIGNAL, id: 's2', lead_id: null }]));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s2'));
    expect(await screen.findByTestId('signal-no-contact')).toBeInTheDocument();
    // No Draft & send button on a contactless signal card.
    expect(screen.queryByTestId('draft-signal-s2')).toBeNull();
  });
});

describe('Signals → add to campaign (bulk)', () => {
  it('selecting signals reveals the bar; add calls the API and excludes complete campaigns', async () => {
    api.addSignalsToCampaign.mockResolvedValue({
      added: 1, skipped_duplicate: 0, skipped_suppressed: 0,
      skipped_no_contact: 0, research_started: true, signals_actioned: 1,
    });
    const user = userEvent.setup();
    renderPage();

    await screen.findByTestId('select-signal-s1');
    expect(screen.queryByTestId('signal-add-to-campaign-bar')).toBeNull();

    await user.click(screen.getByTestId('select-signal-s1'));
    const bar = screen.getByTestId('signal-add-to-campaign-bar');
    expect(bar).toHaveTextContent('1 signal selected');

    const select = within(bar).getByTestId('signal-target-campaign');
    expect(within(select).queryByText(/Old one/)).toBeNull();   // complete excluded
    expect(within(bar).getByTestId('signal-add-to-campaign-btn')).toBeDisabled();

    await user.selectOptions(select, 'c1');
    await user.click(within(bar).getByTestId('signal-add-to-campaign-btn'));
    await waitFor(() => {
      expect(api.addSignalsToCampaign).toHaveBeenCalled();
      const payload = api.addSignalsToCampaign.mock.calls[0][0];
      expect(payload.signal_ids).toEqual(['s1']);
      expect(payload.campaign_id).toBe('c1');
    });
    // Bar clears after success.
    await waitFor(() => expect(screen.queryByTestId('signal-add-to-campaign-bar')).toBeNull());
  });

  it('selecting a signal does not open the detail modal', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('select-signal-s1'));
    expect(screen.queryByTestId('signal-detail-modal')).toBeNull();
  });
});

describe('Signals → find contact (enrichment)', () => {
  const NO_CONTACT = { ...SIGNAL, id: 's2', lead_id: null };

  it('card shows Find contact instead of Draft & send; click calls the API', async () => {
    api.listSignals.mockResolvedValue(paged([NO_CONTACT]));
    api.enrichSignalContact.mockResolvedValue({
      found: true, email: 'ed@helpinghands.org', generic: false,
      lead_id: 'l9', lead_created: true, already_had_contact: false,
    });
    const user = userEvent.setup();
    renderPage();

    await screen.findByTestId('signal-card-s2');
    expect(screen.queryByTestId('draft-signal-s2')).toBeNull();
    await user.click(screen.getByTestId('enrich-signal-s2'));
    await waitFor(() => {
      expect(api.enrichSignalContact).toHaveBeenCalledWith('s2');
    });
  });

  it('modal Find contact resolves an emailable contact and reveals the draft flow', async () => {
    api.listSignals.mockResolvedValue(paged([NO_CONTACT]));
    api.enrichSignalContact.mockResolvedValue({
      found: true, has_email: true, email: 'ed@helpinghands.org', generic: false,
      lead_id: 'l9', lead_created: true, already_had_contact: false,
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('signal-card-s2'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.click(within(modal).getByTestId('signal-enrich-btn'));
    await waitFor(() => {
      expect(api.enrichSignalContact).toHaveBeenCalledWith('s2');
    });
    // Emailable contact → draft button available, no-contact panel gone.
    expect(await within(modal).findByTestId('signal-draft-btn')).toBeInTheDocument();
    expect(within(modal).queryByTestId('signal-no-contact')).toBeNull();
  });

  it('creates a LinkedIn-only lead (no email) and surfaces the profile', async () => {
    api.listSignals.mockResolvedValue(paged([NO_CONTACT]));
    api.enrichSignalContact.mockResolvedValue({
      found: true, has_email: false, email: null,
      lead_id: 'l9', lead_created: true, already_had_contact: false,
      linkedin_url: 'https://www.linkedin.com/in/dana-reed',
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('signal-card-s2'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.click(within(modal).getByTestId('signal-enrich-btn'));
    // Lead created but not emailable → LinkedIn-only panel, no draft flow.
    const panel = await within(modal).findByTestId('signal-linkedin-only');
    expect(within(panel).getByTestId('signal-found-linkedin'))
      .toHaveAttribute('href', 'https://www.linkedin.com/in/dana-reed');
    expect(within(modal).queryByTestId('signal-draft-btn')).toBeNull();
  });

  it('a lead with no email shows View lead (not Draft & send) on the card', async () => {
    api.listSignals.mockResolvedValue(paged([
      { ...SIGNAL, id: 's3', lead_id: 'l3', lead_has_email: false },
    ]));
    renderPage();
    await screen.findByTestId('signal-card-s3');
    expect(screen.queryByTestId('draft-signal-s3')).toBeNull();
    expect(screen.getByTestId('view-lead-signal-s3')).toBeInTheDocument();
  });
});

describe('Signal detail → lead actions (opportunity / campaign)', () => {
  it('Create opportunity converts the lead and navigates to the deal', async () => {
    crmApi.convertLead.mockResolvedValue({ opportunity_id: 'opp9' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.click(within(modal).getByTestId('signal-create-opp-btn'));
    await waitFor(() => {
      expect(crmApi.convertLead).toHaveBeenCalledWith('l1');
      expect(mockNavigate).toHaveBeenCalledWith('/opportunities/opp9');
    });
  });

  it('Add to campaign posts the signal id + chosen campaign', async () => {
    api.addSignalsToCampaign.mockResolvedValue({ added: 1, research_started: true });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');
    const sel = within(modal).getByTestId('signal-add-campaign-select');
    expect(within(modal).getByTestId('signal-add-campaign-btn')).toBeDisabled();
    await user.selectOptions(sel, 'c1');
    await user.click(within(modal).getByTestId('signal-add-campaign-btn'));
    await waitFor(() => {
      const payload = api.addSignalsToCampaign.mock.calls[0][0];
      expect(payload.signal_ids).toEqual(['s1']);
      expect(payload.campaign_id).toBe('c1');
    });
  });

  it('lead actions are hidden for a contactless signal', async () => {
    api.listSignals.mockResolvedValue(paged([{ ...SIGNAL, id: 's2', lead_id: null }]));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s2'));
    const modal = await screen.findByTestId('signal-detail-modal');
    expect(within(modal).queryByTestId('signal-lead-actions')).toBeNull();
  });
});

describe('Signal detail → contact & website info', () => {
  it('shows the resolved contact + a clickable website link', async () => {
    api.listSignals.mockResolvedValue(paged([{
      ...SIGNAL, id: 's5',
      detail: { website: 'helpinghands.org', amount: 50000 },
      lead: {
        email: 'dana@helpinghands.org', first_name: 'Dana', last_name: 'Reed',
        job_title: 'Executive Director', company: 'Helping Hands',
        company_website: 'https://helpinghands.org',
        linkedin_url: 'https://www.linkedin.com/in/dana-reed',
      },
    }]));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s5'));
    const modal = await screen.findByTestId('signal-detail-modal');
    const info = within(modal).getByTestId('signal-contact-info');
    expect(info).toHaveTextContent('Dana Reed');
    expect(info).toHaveTextContent('Executive Director');
    expect(info).toHaveTextContent('dana@helpinghands.org');
    const link = within(info).getByTestId('signal-website-link');
    expect(link).toHaveAttribute('href', 'https://helpinghands.org');
    expect(within(info).getByTestId('signal-linkedin-link')).toBeInTheDocument();
  });

  it('builds an https href for a bare domain and hides the panel with no info', async () => {
    api.listSignals.mockResolvedValue(paged([
      { ...SIGNAL, id: 's6', detail: { website: 'bare-domain.org' }, lead: null },
      { ...SIGNAL, id: 's7', detail: {}, lead: null },
    ]));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s6'));
    let modal = await screen.findByTestId('signal-detail-modal');
    expect(within(modal).getByTestId('signal-website-link'))
      .toHaveAttribute('href', 'https://bare-domain.org');
    await user.click(within(modal).getByLabelText('Close'));

    await user.click(await screen.findByTestId('signal-card-s7'));
    modal = await screen.findByTestId('signal-detail-modal');
    expect(within(modal).queryByTestId('signal-contact-info')).toBeNull();
  });
});

describe('Signal draft → configurable prompt + signature', () => {
  it('draft sends the configured prompt + tone', async () => {
    api.draftSignalEmail.mockResolvedValue({
      to_email: 'jane@acme.com', to_name: 'Jane', subject: 'S', body: 'B',
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.type(within(modal).getByTestId('signal-prompt'), 'focus on the grant');
    await user.type(within(modal).getByTestId('signal-tone'), 'casual');
    await user.click(within(modal).getByTestId('signal-draft-btn'));
    await waitFor(() => {
      const [id, payload] = api.draftSignalEmail.mock.calls[0];
      expect(id).toBe('s1');
      expect(payload).toEqual({ goal: 'focus on the grant', tone: 'casual' });
    });
  });

  it('send includes an edited signature override', async () => {
    api.draftSignalEmail.mockResolvedValue({
      to_email: 'jane@acme.com', to_name: 'Jane', subject: 'S', body: 'B',
    });
    api.sendSignalEmail.mockResolvedValue({
      message_id: 'm1', crm_activity_logged: false, signal_status: 'actioned',
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.click(within(modal).getByTestId('signal-draft-btn'));
    const sig = await within(modal).findByTestId('signal-signature');
    // Pre-filled from the account ('Anthony'), then overridden.
    expect(sig).toHaveValue('Anthony');
    await user.clear(sig);
    await user.type(sig, 'Custom Sig\nGrantMind');
    await user.click(within(modal).getByTestId('signal-send-btn'));
    await waitFor(() => {
      const payload = api.sendSignalEmail.mock.calls[0][1];
      expect(payload.signature).toBe('Custom Sig\nGrantMind');
    });
  });

  it('send omits signature when the account default is left untouched', async () => {
    api.draftSignalEmail.mockResolvedValue({
      to_email: 'jane@acme.com', to_name: 'Jane', subject: 'S', body: 'B',
    });
    api.sendSignalEmail.mockResolvedValue({
      message_id: 'm1', crm_activity_logged: false, signal_status: 'actioned',
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('signal-card-s1'));
    const modal = await screen.findByTestId('signal-detail-modal');
    await user.click(within(modal).getByTestId('signal-draft-btn'));
    await within(modal).findByTestId('signal-signature');
    await user.click(within(modal).getByTestId('signal-send-btn'));
    await waitFor(() => {
      const payload = api.sendSignalEmail.mock.calls[0][1];
      expect('signature' in payload).toBe(false);   // backend uses account default
    });
  });
});
