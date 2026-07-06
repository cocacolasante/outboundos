import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Settings from './Settings.jsx';
import { ToastProvider } from '../components/Toast.jsx';

vi.mock('../api/connectedAccounts.js', () => ({
  listAccounts: vi.fn(),
  getAccount: vi.fn(),
  createAccount: vi.fn(),
  updateAccount: vi.fn(),
  deleteAccount: vi.fn(),
  testAccount: vi.fn(),
  getAccountStatus: vi.fn(),
}));
vi.mock('../api/settings.js', () => ({
  getApiStatus: vi.fn(),
  listIntegrations: vi.fn().mockResolvedValue([]),
  saveIntegration: vi.fn(),
  testIntegration: vi.fn(),
  deleteIntegration: vi.fn(),
  registerUnipileWebhooks: vi.fn(),
}));
vi.mock('../api/auth.js', () => ({
  getMe: vi.fn().mockResolvedValue({
    user_id: 'u-1', email: 'owner@example.com', tenant_id: 't-1',
    tenant_name: 'Test workspace', tenant_slug: 'test-workspace', role: 'owner',
  }),
  listTeam: vi.fn().mockResolvedValue([]),
  inviteMember: vi.fn(),
  removeMember: vi.fn(),
  renameTenant: vi.fn(),
}));
vi.mock('../api/billing.js', () => ({
  getBilling: vi.fn(),
  createCheckout: vi.fn(),
  createPortal: vi.fn(),
}));
vi.mock('../api/agent.js', () => ({
  getAgentSettings: vi.fn(),
  updateAgentSettings: vi.fn(),
}));
vi.mock('../api/signals.js', () => ({
  listFundingSources: vi.fn(),
  updateFundingSource: vi.fn(),
  runFundingSourceNow: vi.fn(),
  stopFundingSource: vi.fn(),
}));

import * as accountsApi from '../api/connectedAccounts.js';
import * as settingsApi from '../api/settings.js';
import * as agentApi from '../api/agent.js';
import * as authApi from '../api/auth.js';
import * as billingApi from '../api/billing.js';
import * as signalsApi from '../api/signals.js';

const FUNDING_SOURCES = {
  hunter_configured: false,
  sources: [
    {
      source: 'usaspending', label: 'USASpending', enabled: true,
      config: { lookback_days: 7 }, last_run_at: '2026-06-15T04:00:00Z',
      last_run_status: 'done', cursor: {}, signal_count: 12,
    },
    {
      source: 'irs_bmf', label: 'IRS BMF', enabled: true,
      config: { ruling_lookback_months: 2, states: ['PA', 'NJ'] },
      last_run_at: null, last_run_status: null, cursor: {}, signal_count: 0,
    },
  ],
};

const AGENT_SETTINGS = {
  auto_log_replies: true,
  auto_create_convert_reminders: true,
  auto_draft_replies: false,
  stale_opp_nudges_enabled: true,
  daily_digest_enabled: true,
  notify_on_positive_reply: true,
  notify_on_any_reply: false,
  min_confidence_to_act: 0.6,
  quiet_hours_start_utc: null,
  quiet_hours_end_utc: null,
  agent_enabled: true,
  owner_email_configured: false,
  updated_at: '2026-06-12T10:00:00Z',
};

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider defaultDuration={0}>
        <Settings />
      </ToastProvider>
    </QueryClientProvider>
  );
}

const sampleAccount = {
  id: 'acc-1',
  label: 'Work Gmail',
  email_address: 'me@gmail.com',
  imap_host: 'imap.gmail.com',
  imap_port: 993,
  imap_use_ssl: true,
  username: 'me@gmail.com',
  last_tested_at: '2026-05-12T10:00:00Z',
  last_test_status: 'ok',
  last_test_error: null,
  last_polled_at: null,
  created_at: '2026-05-12T09:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  accountsApi.listAccounts.mockResolvedValue([]);
  settingsApi.getApiStatus.mockResolvedValue({
    anthropic: true,
    brevo: true,
    apollo: false,
    hunter: false,
  });
  agentApi.getAgentSettings.mockResolvedValue(AGENT_SETTINGS);
  signalsApi.listFundingSources.mockResolvedValue(FUNDING_SOURCES);
});

describe('Settings page tabs', () => {
  it('renders both tabs and starts on Connected inboxes', async () => {
    renderSettings();
    expect(screen.getByRole('tab', { name: /connected inboxes/i })).toHaveAttribute(
      'aria-selected', 'true'
    );
    expect(screen.getByRole('tab', { name: /api status/i })).toHaveAttribute(
      'aria-selected', 'false'
    );
    await waitFor(() => {
      expect(screen.getByTestId('empty-state')).toBeInTheDocument();
    });
  });

  it('switches to API status tab and shows configured/unconfigured rows', async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: /api status/i }));

    await waitFor(() => {
      expect(screen.getByTestId('api-row-anthropic')).toBeInTheDocument();
    });
    expect(screen.getByTestId('api-row-anthropic')).toHaveTextContent(/configured/i);
    expect(screen.getByTestId('api-row-brevo')).toHaveTextContent(/configured/i);
    expect(screen.getByTestId('api-row-apollo')).toHaveTextContent(/not configured/i);
    expect(screen.getByTestId('api-row-hunter')).toHaveTextContent(/not configured/i);
  });

  it('marks optional integrations as optional', async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: /api status/i }));
    await waitFor(() => screen.getByTestId('api-row-apollo'));
    expect(screen.getByTestId('api-row-apollo')).toHaveTextContent(/optional/i);
    expect(screen.getByTestId('api-row-hunter')).toHaveTextContent(/optional/i);
    expect(screen.getByTestId('api-row-anthropic')).not.toHaveTextContent(/optional/i);
  });
});

describe('Connected inboxes list', () => {
  it('renders account cards with status badge', async () => {
    accountsApi.listAccounts.mockResolvedValue([sampleAccount]);
    renderSettings();
    await waitFor(() => screen.getByTestId('account-card'));
    expect(screen.getByText('Work Gmail')).toBeInTheDocument();
    expect(screen.getByText(/me@gmail.com/)).toBeInTheDocument();
    expect(screen.getByTestId('status-badge')).toHaveAttribute('data-status', 'ok');
  });

  it('shows failure error when account is failed', async () => {
    accountsApi.listAccounts.mockResolvedValue([{
      ...sampleAccount,
      last_test_status: 'failed',
      last_test_error: 'authentication failed',
    }]);
    renderSettings();
    await waitFor(() => screen.getByTestId('account-card'));
    expect(screen.getByText(/authentication failed/i)).toBeInTheDocument();
    expect(screen.getByTestId('status-badge')).toHaveAttribute('data-status', 'failed');
  });

  it('opens the modal when "Connect inbox" is clicked', async () => {
    const user = userEvent.setup();
    renderSettings();
    await waitFor(() => screen.getByTestId('empty-state'));
    await user.click(screen.getByRole('button', { name: /connect inbox/i }));
    expect(await screen.findByRole('heading', { name: /connect inbox/i })).toBeInTheDocument();
  });

  it('calls test mutation when Test button clicked', async () => {
    accountsApi.listAccounts.mockResolvedValue([sampleAccount]);
    accountsApi.testAccount.mockResolvedValue({ ok: true, message_count: 5 });
    renderSettings();
    await screen.findByTestId('account-card');

    await act(async () => {
      fireEvent.click(screen.getByText('Test'));
      // Let the react-query mutation queue flush.
      await Promise.resolve();
      await Promise.resolve();
    });

    // React Query 5 passes a context object as the 2nd arg — just check the lead id.
    expect(accountsApi.testAccount).toHaveBeenCalled();
    expect(accountsApi.testAccount.mock.calls[0][0]).toBe('acc-1');
  });

  it('shows the Default sender badge on a default account + hides the Set button there', async () => {
    accountsApi.listAccounts.mockResolvedValue([
      { ...sampleAccount, id: 'a1', label: 'Default inbox', is_default_sender: true },
      { ...sampleAccount, id: 'a2', label: 'Other inbox', email_address: 'other@x.com', is_default_sender: false },
    ]);
    renderSettings();
    await screen.findAllByTestId('account-card');

    // Badge present on the default row, absent on the other.
    expect(screen.getByTestId('default-sender-badge-a1')).toBeInTheDocument();
    expect(screen.queryByTestId('default-sender-badge-a2')).toBeNull();
    // The "Set as default sender" button only appears on the non-default
    // row — the default one doesn't show the button (you can't re-promote
    // the already-default).
    expect(screen.queryByTestId('set-default-sender-a1')).toBeNull();
    expect(screen.getByTestId('set-default-sender-a2')).toBeInTheDocument();
    // Explainer paragraph also shows because there are 2+ accounts.
    expect(screen.getByTestId('default-sender-explainer')).toBeInTheDocument();
  });

  it('clicking Set as default sender calls updateAccount with is_default_sender=true', async () => {
    accountsApi.listAccounts.mockResolvedValue([
      { ...sampleAccount, id: 'a1', label: 'A', is_default_sender: true },
      { ...sampleAccount, id: 'a2', label: 'B', email_address: 'b@x.com', is_default_sender: false },
    ]);
    accountsApi.updateAccount.mockResolvedValue({
      ...sampleAccount, id: 'a2', is_default_sender: true,
    });
    renderSettings();
    await screen.findAllByTestId('account-card');

    await act(async () => {
      fireEvent.click(screen.getByTestId('set-default-sender-a2'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(accountsApi.updateAccount).toHaveBeenCalledTimes(1);
    expect(accountsApi.updateAccount.mock.calls[0][0]).toBe('a2');
    expect(accountsApi.updateAccount.mock.calls[0][1]).toEqual({ is_default_sender: true });
  });

  it('explainer paragraph is hidden when only one inbox exists', async () => {
    accountsApi.listAccounts.mockResolvedValue([
      { ...sampleAccount, id: 'a1', is_default_sender: false },
    ]);
    renderSettings();
    await screen.findByTestId('account-card');
    expect(screen.queryByTestId('default-sender-explainer')).toBeNull();
  });
});

describe('Agent tab', () => {
  it('renders the toggles + owner-email warning when not configured', async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Agent' }));

    expect(await screen.findByTestId('agent-tab')).toBeInTheDocument();
    expect(screen.getByTestId('agent-toggle-auto_log_replies')).toBeInTheDocument();
    expect(screen.getByTestId('agent-toggle-daily_digest_enabled')).toBeInTheDocument();
    // OWNER_NOTIFY_EMAIL not configured → amber warning.
    expect(screen.getByTestId('owner-email-status')).toHaveTextContent(/not set/i);
    // Kill-switch banner NOT shown when agent_enabled.
    expect(screen.queryByTestId('agent-killswitch-banner')).toBeNull();
  });

  it('toggling a checkbox PATCHes the setting', async () => {
    agentApi.updateAgentSettings.mockResolvedValue({
      ...AGENT_SETTINGS, auto_draft_replies: true,
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Agent' }));
    await screen.findByTestId('agent-tab');

    const label = screen.getByTestId('agent-toggle-auto_draft_replies');
    await user.click(label.querySelector('input[type="checkbox"]'));
    await waitFor(() => {
      // React Query 5 passes a context object as the 2nd arg — check the payload only.
      expect(agentApi.updateAgentSettings).toHaveBeenCalled();
      expect(agentApi.updateAgentSettings.mock.calls[0][0]).toEqual({ auto_draft_replies: true });
    });
  });

  it('shows the kill-switch banner when AGENT_ENABLED is off', async () => {
    agentApi.getAgentSettings.mockResolvedValue({
      ...AGENT_SETTINGS, agent_enabled: false,
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Agent' }));
    expect(await screen.findByTestId('agent-killswitch-banner')).toBeInTheDocument();
  });
});

describe('Discovery tab', () => {
  it('renders both feed cards with config + last-run + signal counts', async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));

    expect(await screen.findByTestId('discovery-tab')).toBeInTheDocument();
    const usa = screen.getByTestId('funding-card-usaspending');
    expect(usa).toHaveTextContent('USASpending');
    expect(usa).toHaveTextContent('12 signals surfaced');
    const irs = screen.getByTestId('funding-card-irs_bmf');
    expect(irs).toHaveTextContent('IRS BMF');
    // IRS states prefilled from config.
    expect(screen.getByTestId('funding-states')).toHaveValue('PA, NJ');
    // Hunter not configured → warning banner.
    expect(screen.getByTestId('hunter-warning')).toBeInTheDocument();
  });

  it('saving the IRS feed PATCHes enabled + states + ruling months', async () => {
    signalsApi.updateFundingSource.mockResolvedValue({
      ...FUNDING_SOURCES.sources[1], config: { ruling_lookback_months: 2, states: ['PA', 'NJ', 'NY'] },
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await screen.findByTestId('funding-card-irs_bmf');

    const states = screen.getByTestId('funding-states');
    await user.clear(states);
    await user.type(states, 'PA, NJ, NY');
    await user.click(screen.getByTestId('funding-save-irs_bmf'));

    await waitFor(() => {
      expect(signalsApi.updateFundingSource).toHaveBeenCalled();
      const [src, payload] = signalsApi.updateFundingSource.mock.calls[0];
      expect(src).toBe('irs_bmf');
      expect(payload.states).toEqual(['PA', 'NJ', 'NY']);
      expect(payload.enabled).toBe(true);
      expect(payload.ruling_lookback_months).toBe(2);
    });
  });

  it('saving USASpending PATCHes the max award amount (blank = no cap)', async () => {
    signalsApi.updateFundingSource.mockResolvedValue(FUNDING_SOURCES.sources[0]);
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await screen.findByTestId('funding-card-usaspending');

    const maxAward = screen.getByTestId('funding-max-award');
    await user.clear(maxAward);
    await user.type(maxAward, '250000');
    await user.click(screen.getByTestId('funding-save-usaspending'));
    await waitFor(() => {
      const [src, payload] = signalsApi.updateFundingSource.mock.calls[0];
      expect(src).toBe('usaspending');
      expect(payload.max_award_amount).toBe(250000);
    });

    // Blank clears the cap → explicit null.
    signalsApi.updateFundingSource.mockClear();
    await user.clear(maxAward);
    await user.click(screen.getByTestId('funding-save-usaspending'));
    await waitFor(() => {
      expect(signalsApi.updateFundingSource.mock.calls[0][1].max_award_amount).toBeNull();
    });
  });

  it('saving a feed PATCHes max_per_run (lead-pull cap)', async () => {
    signalsApi.updateFundingSource.mockResolvedValue(FUNDING_SOURCES.sources[1]);
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await screen.findByTestId('funding-card-irs_bmf');

    const cap = screen.getByTestId('funding-max-per-run-irs_bmf');
    await user.clear(cap);
    await user.type(cap, '40');
    await user.click(screen.getByTestId('funding-save-irs_bmf'));
    await waitFor(() => {
      const [src, payload] = signalsApi.updateFundingSource.mock.calls[0];
      expect(src).toBe('irs_bmf');
      expect(payload.max_per_run).toBe(40);
    });
  });

  it('Run now calls the API for an enabled feed', async () => {
    signalsApi.runFundingSourceNow.mockResolvedValue({ enqueued: true });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await user.click(await screen.findByTestId('funding-run-usaspending'));
    await waitFor(() => {
      expect(signalsApi.runFundingSourceNow).toHaveBeenCalledWith('usaspending');
    });
  });

  it('Stop calls the API and reports the terminated run', async () => {
    signalsApi.stopFundingSource.mockResolvedValue({
      ...FUNDING_SOURCES.sources[1], last_run_status: 'stopped',
      stopped: { terminated: ['abc'], purged_queued: 2, purged_unacked: 1 },
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await user.click(await screen.findByTestId('funding-stop-irs_bmf'));
    await waitFor(() => {
      expect(signalsApi.stopFundingSource).toHaveBeenCalledWith('irs_bmf');
    });
    expect(await screen.findByText(/run stopped/i)).toBeInTheDocument();
  });

  it('Stop on an idle feed reports nothing was running', async () => {
    signalsApi.stopFundingSource.mockResolvedValue({
      ...FUNDING_SOURCES.sources[0],
      stopped: { terminated: [], purged_queued: 0, purged_unacked: 0 },
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await user.click(await screen.findByTestId('funding-stop-usaspending'));
    expect(await screen.findByText(/No .* run was in progress/i)).toBeInTheDocument();
  });

  it('hides the Hunter warning when configured', async () => {
    signalsApi.listFundingSources.mockResolvedValue({
      ...FUNDING_SOURCES, hunter_configured: true,
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Discovery' }));
    await screen.findByTestId('discovery-tab');
    expect(screen.queryByTestId('hunter-warning')).toBeNull();
  });
});

describe('Integrations tab (BYOK)', () => {
  it('lists providers with masked keys and saves a new key', async () => {
    settingsApi.listIntegrations.mockResolvedValue([
      { provider: 'anthropic', configured: true, masked: '••••1234',
        last_test_status: 'ok', last_tested_at: null, last_test_error: null },
      { provider: 'brevo', configured: false, masked: null,
        last_test_status: null, last_tested_at: null, last_test_error: null },
      { provider: 'apollo', configured: false, masked: null,
        last_test_status: null, last_tested_at: null, last_test_error: null },
      { provider: 'hunter', configured: false, masked: null,
        last_test_status: null, last_tested_at: null, last_test_error: null },
      { provider: 'unipile', configured: false, masked: null,
        last_test_status: null, last_tested_at: null, last_test_error: null },
    ]);
    settingsApi.saveIntegration.mockResolvedValue({
      provider: 'brevo', configured: true, masked: '••••5678',
      last_test_status: 'untested',
    });
    const user = userEvent.setup();
    renderSettings();

    await user.click(screen.getByRole('tab', { name: 'Integrations' }));
    await screen.findByTestId('integrations-tab');
    expect(screen.getByTestId('integration-anthropic')).toHaveTextContent('••••1234');

    await user.click(screen.getByTestId('integration-brevo-edit'));
    await user.type(screen.getByTestId('integration-brevo-key'), 'xkeysmash-5678');
    await user.type(screen.getByTestId('integration-brevo-sender_email'), 'me@acme.com');
    await user.click(screen.getByTestId('integration-brevo-save'));

    await waitFor(() => expect(settingsApi.saveIntegration).toHaveBeenCalledWith('brevo', {
      api_key: 'xkeysmash-5678', sender_email: 'me@acme.com',
    }));
  });

  it('test button reports the persisted status', async () => {
    settingsApi.listIntegrations.mockResolvedValue([
      { provider: 'anthropic', configured: true, masked: '••••1234',
        last_test_status: 'untested', last_tested_at: null, last_test_error: null },
    ]);
    settingsApi.testIntegration.mockResolvedValue({
      provider: 'anthropic', configured: true, masked: '••••1234',
      last_test_status: 'failed', last_test_error: '401 unauthorized',
    });
    const user = userEvent.setup();
    renderSettings();

    await user.click(screen.getByRole('tab', { name: 'Integrations' }));
    await screen.findByTestId('integration-anthropic');
    await user.click(screen.getByTestId('integration-anthropic-test'));
    await waitFor(() => expect(settingsApi.testIntegration).toHaveBeenCalledWith('anthropic'));
  });
});

describe('Billing tab', () => {
  it('shows plan, trial countdown, usage bars and upgrade buttons', async () => {
    billingApi.getBilling.mockResolvedValue({
      plan: 'starter',
      subscription_status: 'trialing',
      trial_ends_at: new Date(Date.now() + 5 * 86400000).toISOString(),
      current_period_end: null,
      spend_allowed: true,
      blocked_reason: '',
      usage: { email_send: { used: 250, quota: 1000 } },
      billing_configured: true,
    });
    billingApi.createCheckout.mockResolvedValue({ url: 'https://stripe.test/checkout' });
    const user = userEvent.setup();
    renderSettings();

    await user.click(screen.getByRole('tab', { name: 'Billing' }));
    await screen.findByTestId('billing-tab');
    expect(screen.getByTestId('trial-countdown')).toHaveTextContent(/5 days/i);
    expect(screen.getByTestId('usage-email_send')).toHaveTextContent('250 / 1,000');
    expect(screen.queryByTestId('billing-blocked-banner')).toBeNull();

    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, 'location', {
      value: { ...original, assign }, writable: true,
    });
    await user.click(screen.getByTestId('billing-upgrade-pro'));
    await waitFor(() => expect(billingApi.createCheckout).toHaveBeenCalledWith('pro'));
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://stripe.test/checkout'));
    Object.defineProperty(window, 'location', { value: original, writable: true });
  });

  it('shows the blocked banner when spend is denied', async () => {
    billingApi.getBilling.mockResolvedValue({
      plan: 'starter',
      subscription_status: 'canceled',
      trial_ends_at: null,
      current_period_end: null,
      spend_allowed: false,
      blocked_reason: 'subscription canceled — subscribe to keep sending',
      usage: {},
      billing_configured: true,
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Billing' }));
    expect(await screen.findByTestId('billing-blocked-banner')).toHaveTextContent(/canceled/i);
  });
});

describe('Workspace tab', () => {
  it('lists team, invites a member, and renames the workspace', async () => {
    authApi.listTeam.mockResolvedValue([
      { membership_id: 'm-1', email: 'owner@example.com', role: 'owner', pending: false },
      { membership_id: 'm-2', email: 'pending@example.com', role: 'member', pending: true },
    ]);
    authApi.inviteMember.mockResolvedValue({
      membership_id: 'm-3', email: 'new@example.com', role: 'member', pending: true,
    });
    authApi.renameTenant.mockResolvedValue({ tenant_name: 'Renamed' });
    const user = userEvent.setup();
    renderSettings();

    await user.click(screen.getByRole('tab', { name: 'Workspace' }));
    await screen.findByTestId('team-member-owner@example.com');
    expect(screen.getByTestId('team-member-pending@example.com'))
      .toHaveTextContent(/invite pending/i);

    await user.type(screen.getByTestId('invite-email'), 'new@example.com');
    await user.click(screen.getByTestId('invite-submit'));
    await waitFor(() => expect(authApi.inviteMember).toHaveBeenCalledWith({
      email: 'new@example.com', role: 'member',
    }));

    await user.type(screen.getByTestId('workspace-name-input'), 'Renamed');
    await user.click(screen.getByTestId('workspace-rename'));
    await waitFor(() => expect(authApi.renameTenant).toHaveBeenCalledWith('Renamed'));
  });

  it('surfaces the 402 seat-cap detail on invite', async () => {
    authApi.listTeam.mockResolvedValue([]);
    authApi.inviteMember.mockRejectedValue({
      response: { status: 402, data: { detail: 'quota exceeded: billing — Starter plan allows 1 seats' } },
    });
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole('tab', { name: 'Workspace' }));
    await screen.findByTestId('invite-email');
    await user.type(screen.getByTestId('invite-email'), 'x@example.com');
    await user.click(screen.getByTestId('invite-submit'));
    expect(await screen.findByText(/allows 1 seats/i)).toBeInTheDocument();
  });
});
