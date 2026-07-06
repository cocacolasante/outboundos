import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App.jsx';

// Every feature route sits behind RequireAuth (multi-tenancy Phase 1) —
// resolve the session so route tests render their pages.
vi.mock('./api/auth.js', () => ({
  getMe: vi.fn().mockResolvedValue({
    user_id: 'u-1',
    email: 'owner@example.com',
    tenant_id: 't-1',
    tenant_name: 'Test workspace',
    tenant_slug: 'test-workspace',
    role: 'owner',
  }),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  forgotPassword: vi.fn(),
  resetPassword: vi.fn(),
  acceptInvite: vi.fn(),
  listTeam: vi.fn().mockResolvedValue([]),
  inviteMember: vi.fn(),
  removeMember: vi.fn(),
  renameTenant: vi.fn(),
}));
vi.mock('./api/billing.js', () => ({
  getBilling: vi.fn().mockResolvedValue({
    plan: null, subscription_status: null, trial_ends_at: null,
    current_period_end: null, spend_allowed: true, blocked_reason: '',
    usage: {}, billing_configured: false,
  }),
  createCheckout: vi.fn(),
  createPortal: vi.fn(),
}));

// Settings now does data fetching; stub the API modules so route tests don't
// hit the network.
vi.mock('./api/connectedAccounts.js', () => ({
  listAccounts: vi.fn().mockResolvedValue([]),
  getAccount: vi.fn(),
  createAccount: vi.fn(),
  updateAccount: vi.fn(),
  deleteAccount: vi.fn(),
  testAccount: vi.fn(),
  getAccountStatus: vi.fn(),
}));
vi.mock('./api/settings.js', () => ({
  listIntegrations: vi.fn().mockResolvedValue([]),
  saveIntegration: vi.fn(),
  testIntegration: vi.fn(),
  deleteIntegration: vi.fn(),
  getApiStatus: vi.fn().mockResolvedValue({
    anthropic: false, brevo: false, apollo: false, hunter: false,
  }),
}));
vi.mock('./api/campaigns.js', () => ({
  listCampaigns: vi.fn().mockResolvedValue([]),
  getCampaign: vi.fn().mockResolvedValue({
    id: 'abc-123',
    name: 'Sample campaign',
    status: 'draft',
    goal: 'g', tone: 't',
    sender_name: 'A', sender_email: 'a@x.com',
    research_mode: 'fast', sample_count: 5,
    schedule_time_start: '09:00:00', schedule_time_end: '17:00:00', schedule_timezone: 'UTC',
    connected_account_configured: false, connected_account: null,
    lead_counts: { total: 0, pending: 0, scheduled: 0, sent: 0, failed: 0 },
    stats: { open_rate: null, click_rate: null, reply_rate: null, bounce_rate: null },
    created_at: '2026-05-01T12:00:00Z',
  }),
  createCampaign: vi.fn(),
  updateCampaign: vi.fn(),
  deleteCampaign: vi.fn(),
  pauseCampaign: vi.fn(),
  resumeCampaign: vi.fn(),
  listCampaignLeads: vi.fn().mockResolvedValue({
    items: [], total: 0, page: 1, page_size: 50, total_pages: 0,
  }),
  getPreview: vi.fn().mockResolvedValue({
    campaign_id: 'abc-123', status: 'previewing', samples: [], all_ready: false,
  }),
  getPreviewProgress: vi.fn().mockResolvedValue({
    total_leads: 0, researched: 0, composed: 0, sent: 0, failed: 0,
  }),
  updateSample: vi.fn(),
  approveAll: vi.fn(),
  rejectPreview: vi.fn(),
  uploadLeadsPreview: vi.fn(),
  confirmLeadsUpload: vi.fn(),
  listCampaignErrors: vi.fn().mockResolvedValue([]),
  retryFailedLeads: vi.fn(),
  getAnalytics: vi.fn().mockResolvedValue({
    campaign_id: 'abc-123',
    overview: { total_leads: 0, sent: 0, delivered: 0, opened: 0, clicked: 0, replied: 0, bounced: 0, spam_complaints: 0, unsubscribed: 0 },
    rates: { open_rate: null, click_rate: null, reply_rate: null, bounce_rate: null, spam_rate: null, unsub_rate: null, delivery_rate: null },
    reply_tracking_enabled: false,
    timeline: [],
    research_quality_breakdown: [],
    sender_reputation_score: null,
    best_subject_lines: [],
  }),
}));

function renderAt(path) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe('Phase 1 routing', () => {
  // RequireAuth resolves the (mocked) session asynchronously, so every
  // route assertion awaits the first paint behind the auth gate.
  it('renders nav on every route', async () => {
    renderAt('/');
    expect(await screen.findByTestId('nav')).toBeInTheDocument();
  });

  it('renders Campaigns page at /', async () => {
    renderAt('/');
    expect(await screen.findByRole('heading', { name: /campaigns/i, level: 1 })).toBeInTheDocument();
  });

  it('renders CampaignCreate at /campaigns/new', async () => {
    renderAt('/campaigns/new');
    expect(await screen.findByRole('heading', { name: /new campaign/i })).toBeInTheDocument();
  });

  it('renders CampaignDetail with id param', async () => {
    renderAt('/campaigns/abc-123');
    // CampaignDetail now fetches the campaign and shows its name as the h1.
    expect(await screen.findByRole('heading', { name: /sample campaign/i, level: 1 })).toBeInTheDocument();
  });

  it('renders Preview at /campaigns/:id/preview', async () => {
    renderAt('/campaigns/abc-123/preview');
    // Preview now fetches data; heading is just "Preview".
    expect(await screen.findByRole('heading', { name: /^preview$/i })).toBeInTheDocument();
  });

  it('renders Analytics at /campaigns/:id/analytics', async () => {
    renderAt('/campaigns/abc-123/analytics');
    expect(await screen.findByRole('heading', { name: /^analytics$/i })).toBeInTheDocument();
  });

  it('renders Settings at /settings', async () => {
    renderAt('/settings');
    expect(await screen.findByRole('heading', { name: /settings/i })).toBeInTheDocument();
  });
});

describe('Auth gate', () => {
  it('redirects to /login when the session is dead', async () => {
    const { getMe } = await import('./api/auth.js');
    getMe.mockRejectedValueOnce({ response: { status: 401 } });
    renderAt('/leads');
    expect(await screen.findByTestId('login-form')).toBeInTheDocument();
    expect(screen.queryByTestId('nav')).not.toBeInTheDocument();
  });

  it('renders the login page at /login without the app shell', async () => {
    renderAt('/login');
    expect(await screen.findByTestId('login-form')).toBeInTheDocument();
    expect(screen.queryByTestId('nav')).not.toBeInTheDocument();
  });
});
