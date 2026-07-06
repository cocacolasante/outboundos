import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import IntentTab from './IntentSettings.jsx';
import { ToastProvider } from '../components/Toast.jsx';

vi.mock('../api/intent.js', () => ({
  getIntentStatus: vi.fn(),
  listIntentProfiles: vi.fn(),
  createIntentPreset: vi.fn(),
  updateIntentProfile: vi.fn(),
  activateIntentProfile: vi.fn(),
  deleteIntentProfile: vi.fn(),
  getRankedIntent: vi.fn(),
  seedIntentOrgs: vi.fn(),
  runIntentCollectors: vi.fn(),
  recomputeIntent: vi.fn(),
  promoteIntent: vi.fn(),
}));

import * as api from '../api/intent.js';

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <IntentTab />
      </ToastProvider>
    </QueryClientProvider>
  );
}

const PROFILE = {
  id: 'p1', name: 'GrantMind Pro', is_active: true,
  cause_codes: ['T'], geographies: [], rfp_keywords: ['nonprofit capacity building'],
  size_band_weights: { small: 1.4 }, signal_weights: { dev_role_posted: 1.6 },
  half_life_overrides: {}, half_life_days: 30, max_signal_age_days: 180,
  promotion_threshold: 80,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getRankedIntent.mockResolvedValue([]);
  api.listIntentProfiles.mockResolvedValue([]);
});

describe('IntentTab', () => {
  it('shows the dashboard, the Adzuna warning, and create-profile buttons when none exist', async () => {
    api.getIntentStatus.mockResolvedValue({
      adzuna_configured: false, active_profile_id: null, orgs_total: 7,
      signals_by_type: { rev_drop: 3 }, tiers: { 2: 2 }, draft_campaign_id: null,
      draft_pending_leads: 0,
    });
    renderTab();

    await waitFor(() => expect(screen.getByTestId('stat-orgs')).toHaveTextContent('7'));
    expect(screen.getByTestId('adzuna-warning')).toBeInTheDocument();
    expect(await screen.findByTestId('preset-grantmind')).toBeInTheDocument();
  });

  it('creates a GrantMind preset', async () => {
    api.getIntentStatus.mockResolvedValue({
      adzuna_configured: true, active_profile_id: null, orgs_total: 0,
      signals_by_type: {}, tiers: {}, draft_campaign_id: null, draft_pending_leads: 0,
    });
    api.createIntentPreset.mockResolvedValue(PROFILE);
    renderTab();

    fireEvent.click(await screen.findByTestId('preset-grantmind'));
    await waitFor(() => expect(api.createIntentPreset).toHaveBeenCalledWith('grantmind'));
  });

  it('renders the profile editor + ranked feed when a profile is active', async () => {
    api.getIntentStatus.mockResolvedValue({
      adzuna_configured: true, active_profile_id: 'p1', orgs_total: 5,
      signals_by_type: { new_rfp: 4 }, tiers: { 1: 2, 2: 3 },
      draft_campaign_id: 'camp-1', draft_pending_leads: 2,
    });
    api.listIntentProfiles.mockResolvedValue([PROFILE]);
    api.getRankedIntent.mockResolvedValue([
      { org_id: 'o1', name: 'Brooklyn CF', state: 'NY', ntee_code: 'T31', size_band: 'mid',
        intent_score: 173.8, tier: 1, fit_multiplier: 1.4,
        top_signal_id: 's1', why_now: 'Posted a Development Director role', evidence_url: 'https://x',
        top_signal_type: 'dev_role_posted' },
    ]);
    renderTab();

    // Editor populated from the active profile.
    expect(await screen.findByTestId('profile-name')).toHaveValue('GrantMind Pro');
    expect(screen.getByTestId('profile-threshold')).toHaveValue(80);
    // Ranked feed row with the why-now.
    expect(await screen.findByTestId('intent-feed')).toBeInTheDocument();
    expect(screen.getByText('Brooklyn CF')).toBeInTheDocument();
    expect(screen.getByText(/Posted a Development Director role/)).toBeInTheDocument();
  });

  it('runs recompute + promote actions', async () => {
    api.getIntentStatus.mockResolvedValue({
      adzuna_configured: true, active_profile_id: 'p1', orgs_total: 5,
      signals_by_type: {}, tiers: { 1: 1 }, draft_campaign_id: null, draft_pending_leads: 0,
    });
    api.listIntentProfiles.mockResolvedValue([PROFILE]);
    api.recomputeIntent.mockResolvedValue({ orgs: 5, tier1: 1, tier2: 0, tier3: 4 });
    api.promoteIntent.mockResolvedValue({ promoted: 1, draft_campaign_id: 'camp-1' });
    renderTab();

    fireEvent.click(await screen.findByTestId('recompute-btn'));
    await waitFor(() => expect(api.recomputeIntent).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId('promote-btn'));
    await waitFor(() => expect(api.promoteIntent).toHaveBeenCalled());
  });
});
