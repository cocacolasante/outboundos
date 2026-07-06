import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../components/Toast.jsx';
import CampaignDetail from './CampaignDetail.jsx';

vi.mock('../api/campaigns.js', () => ({
  getCampaign: vi.fn(),
  pauseCampaign: vi.fn(),
  resumeCampaign: vi.fn(),
  listCampaignLeads: vi.fn().mockResolvedValue({
    items: [], total: 0, page: 1, page_size: 50, total_pages: 0,
  }),
  getAnalytics: vi.fn().mockResolvedValue({
    campaign_id: 'c1',
    overview: { total_leads: 0, sent: 0, delivered: 0, opened: 0, clicked: 0, replied: 0, bounced: 0, spam_complaints: 0, unsubscribed: 0 },
    rates: { open_rate: null, click_rate: null, reply_rate: null, bounce_rate: null, spam_rate: null, unsub_rate: null, delivery_rate: null },
    reply_tracking_enabled: false,
    timeline: [],
    research_quality_breakdown: [],
    sender_reputation_score: null,
    best_subject_lines: [],
  }),
  listCampaignErrors: vi.fn().mockResolvedValue([]),
  retryFailedLeads: vi.fn(),
  updateCampaign: vi.fn().mockResolvedValue({}),
  applySignature: vi.fn().mockResolvedValue({ updated: 3 }),
  updateLeadEmail: vi.fn().mockResolvedValue({}),
  findLeadContact: vi.fn().mockResolvedValue({ status: 'resolved', email: 'found@org.org' }),
  getRetargetPreview: vi.fn().mockResolvedValue({
    engaged: 12, by_email_click: 12, by_linkedin_connection: 0,
    existing_retarget_campaigns: [],
  }),
  retargetCampaign: vi.fn().mockResolvedValue({
    target_campaign_id: 'rc1', target_campaign_name: 'Retarget — Spring outreach',
    created: true, engaged: 12, added: 12, skipped_duplicate: 0,
  }),
  previewLeadReply: vi.fn(),
  getLeadDetail: vi.fn(),
  getDeliverability: vi.fn(),
  getCopyInsights: vi.fn(),
  getPreviewProgress: vi.fn(),
  stopCampaignPipeline: vi.fn(),
}));

vi.mock('../api/connectedAccounts.js', () => ({
  listAccounts: vi.fn().mockResolvedValue([
    { id: 'a1', label: 'Work Gmail', email_address: 'work@x.com' },
  ]),
}));
vi.mock('../api/linkedinAccounts.js', () => ({
  listLinkedInAccounts: vi.fn().mockResolvedValue([]),
}));

// AnalyticsContent (rendered on the Analytics tab) pulls sequence analytics.
vi.mock('../api/sequences.js', () => ({
  getSequenceAnalytics: vi.fn().mockResolvedValue({
    campaign_id: 'c1', sequence_id: 's1',
    total_leads: 0, halted: 0, completed: 0, active: 0, pending: 0,
    per_node: [],
  }),
}));

// Recharts stub
vi.mock('recharts', () => {
  const noop = ({ children }) => <div>{children}</div>;
  return {
    LineChart: ({ children }) => <div data-testid="line-chart">{children}</div>,
    Line: () => <div />,
    XAxis: noop, YAxis: noop, CartesianGrid: noop, Tooltip: noop, Legend: noop,
    ResponsiveContainer: noop,
  };
});

import * as api from '../api/campaigns.js';

const RUNNING_CAMPAIGN = {
  id: 'c1', name: 'Spring outreach', status: 'running',
  goal: 'Book demos', tone: 'Direct',
  sender_name: 'Anthony', sender_email: 'a@x.com',
  research_mode: 'fast', sample_count: 5,
  schedule_time_start: '09:00:00', schedule_time_end: '17:00:00',
  schedule_timezone: 'UTC',
  connected_account_configured: true,
  connected_account: { id: 'a1', label: 'Work Gmail', email_address: 'work@x.com' },
  lead_counts: { total: 100, pending: 50, scheduled: 0, sent: 50, failed: 0 },
  stats: { open_rate: 0.5, click_rate: 0.2, reply_rate: 0.05, bounce_rate: 0.01 },
  created_at: '2026-05-01T12:00:00Z',
};


function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <MemoryRouter initialEntries={['/campaigns/c1']}>
          <Routes>
            <Route path="/campaigns/:id" element={<CampaignDetail />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

const DELIVERABILITY = {
  window_hours: 24, sample: 40, delivered: 38,
  hard_bounces: 1, soft_bounces: 0, spam: 0, opens: 12,
  bounce_rate: 0.025, spam_rate: 0, open_rate: 0.31,
  breaker: { enabled: true, min_sample: 20, bounce_threshold: 0.05, spam_threshold: 0.001 },
  domain: {
    domain: 'x.com', hour_used: 12, hour_cap: 100, hour_remaining: 88,
    day_used: 40, day_cap: 500, day_remaining: 460,
  },
  auto_paused_at: null, auto_pause_reason: null,
  send_time_optimization: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getCampaign.mockResolvedValue(RUNNING_CAMPAIGN);
  api.getDeliverability.mockResolvedValue(DELIVERABILITY);
  api.getCopyInsights.mockResolvedValue({
    outcome_counts: { positive: 0, neutral: 0, negative: 0 },
    insights: null, refreshed_at: null, winning_examples: [],
  });
  api.getPreviewProgress.mockResolvedValue({
    total_leads: 100, researched: 100, composed: 100, sent: 50, failed: 0,
    composing: 0, pipeline_active: true,
    goal_updated_at: null, rewrite_total: 0, rewrite_done: 0,
  });
});


describe('CampaignDetail', () => {
  it('renders campaign name and status', async () => {
    renderPage();
    expect(await screen.findByTestId('campaign-name')).toHaveTextContent('Spring outreach');
    // Status badge appears in both the page header and the Overview tab.
    const badges = screen.getAllByTestId('status-badge');
    expect(badges.length).toBeGreaterThan(0);
    badges.forEach((b) => expect(b).toHaveAttribute('data-status', 'running'));
  });

  it('Overview is the default tab', async () => {
    renderPage();
    expect(await screen.findByTestId('overview-tab')).toBeInTheDocument();
    expect(screen.getByTestId('tab-overview')).toHaveAttribute('aria-selected', 'true');
  });

  it('shows connected inbox info on Overview', async () => {
    renderPage();
    const info = await screen.findByTestId('reply-tracking-info');
    expect(info).toHaveTextContent(/work gmail/i);
    expect(info).toHaveTextContent(/work@x.com/i);
  });

  it('shows "not configured" when no inbox attached', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN,
      connected_account_configured: false,
      connected_account: null,
    });
    renderPage();
    const info = await screen.findByTestId('reply-tracking-info');
    expect(info).toHaveTextContent(/not configured/i);
  });

  it('Pause button on running campaign calls pauseCampaign', async () => {
    api.pauseCampaign.mockResolvedValue({});
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('pause-resume-button'));
    await waitFor(() => expect(api.pauseCampaign).toHaveBeenCalledWith('c1'));
  });

  it('Resume button on paused campaign calls resumeCampaign', async () => {
    api.getCampaign.mockResolvedValue({ ...RUNNING_CAMPAIGN, status: 'paused' });
    api.resumeCampaign.mockResolvedValue({});
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('pause-resume-button'));
    await waitFor(() => expect(api.resumeCampaign).toHaveBeenCalledWith('c1'));
  });

  it('No pause/resume button on draft campaign', async () => {
    api.getCampaign.mockResolvedValue({ ...RUNNING_CAMPAIGN, status: 'draft' });
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.queryByTestId('pause-resume-button')).not.toBeInTheDocument();
  });

  it('shows an auto-paused note when paused at the LinkedIn cap', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'paused',
      auto_paused_until: '2026-05-27T13:00:00Z',
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.getByTestId('auto-paused-note')).toHaveTextContent(/auto-paused.*linkedin daily cap/i);
  });

  it('no auto-paused note for a manual pause', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'paused', auto_paused_until: null,
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.queryByTestId('auto-paused-note')).not.toBeInTheDocument();
  });

  it('Switching to Leads tab renders the LeadTable', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    expect(await screen.findByTestId('leads-tab')).toBeInTheDocument();
    expect(screen.getByTestId('lead-table')).toBeInTheDocument();
  });

  it('Switching to Analytics tab renders analytics content', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-analytics'));
    expect(await screen.findByTestId('analytics-tab')).toBeInTheDocument();
    // Analytics content shows metric tiles
    await waitFor(() => {
      expect(screen.getByTestId('metric-sent')).toBeInTheDocument();
    });
  });

  it('campaign config card shows goal/tone/sender/schedule', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.getByText(/book demos/i)).toBeInTheDocument();
    expect(screen.getByText(/direct/i)).toBeInTheDocument();
    expect(screen.getByText(/anthony/i)).toBeInTheDocument();
    expect(screen.getByText(/09:00.*17:00.*utc/i)).toBeInTheDocument();
  });

  it('Leads tab signature editor saves a per-campaign override via updateCampaign', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    const editor = await screen.findByTestId('signature-editor');
    // No override + no account signature → must opt into customizing first.
    fireEvent.click(within(editor).getByTestId('customize-signature-btn'));
    fireEvent.change(within(editor).getByRole('textbox'), {
      target: { value: 'Anthony\n555-1234\nacme.com' },
    });
    fireEvent.click(screen.getByTestId('save-signature-btn'));
    await waitFor(() => expect(api.updateCampaign).toHaveBeenCalledWith('c1', {
      signature: 'Anthony\n555-1234\nacme.com',
    }));
  });

  it('shows the inherited Settings signature when no per-campaign override', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, signature: null,
      account_signature: 'Anthony\nacme.com',
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    const editor = await screen.findByTestId('signature-editor');
    expect(within(editor).getByTestId('signature-inherited-preview').textContent)
      .toMatch(/acme\.com/);
    // No editable textarea until the user opts into customizing.
    expect(within(editor).queryByRole('textbox')).toBeNull();
  });

  it('revert clears the per-campaign override (signature: "")', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, signature: 'Override sig',
      account_signature: 'Acct sig',
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    const editor = await screen.findByTestId('signature-editor');
    fireEvent.click(within(editor).getByTestId('revert-signature-btn'));
    await waitFor(() => expect(api.updateCampaign).toHaveBeenCalledWith('c1', {
      signature: '',
    }));
  });

  it('Goal editor saves a new goal via updateCampaign', async () => {
    api.updateCampaign.mockResolvedValue({ ...RUNNING_CAMPAIGN, goal: 'New goal' });
    renderPage();
    await screen.findByTestId('overview-tab');
    const editor = await screen.findByTestId('goal-editor');
    expect(within(editor).getByTestId('goal-value').textContent).toMatch(/Book demos/);
    fireEvent.click(within(editor).getByTestId('edit-goal-btn'));
    fireEvent.change(within(editor).getByTestId('goal-input'), {
      target: { value: 'Book product walkthroughs' },
    });
    fireEvent.click(within(editor).getByTestId('save-goal-btn'));
    await waitFor(() => expect(api.updateCampaign).toHaveBeenCalledWith('c1', {
      goal: 'Book product walkthroughs',
    }));
  });

  it('Goal editor is read-only (no Edit) on a completed campaign', async () => {
    api.getCampaign.mockResolvedValue({ ...RUNNING_CAMPAIGN, status: 'complete' });
    renderPage();
    await screen.findByTestId('overview-tab');
    const editor = await screen.findByTestId('goal-editor');
    expect(within(editor).queryByTestId('edit-goal-btn')).toBeNull();
  });

  it('Schedule editor renders a read-only summary on overview', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN,
      schedule_days: [1, 2, 3, 4, 5],
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    const editor = await screen.findByTestId('schedule-editor');
    const summary = within(editor).getByTestId('schedule-summary');
    expect(summary.textContent).toMatch(/09:00.*17:00.*UTC/);
    expect(summary.textContent).toMatch(/Mon, Tue, Wed, Thu, Fri/);
  });

  it('Schedule editor saves a new window via updateCampaign on a running campaign', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN,
      schedule_days: [1, 2, 3, 4, 5],
      min_delay_seconds: 60,
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(await screen.findByTestId('schedule-editor-toggle'));

    fireEvent.change(screen.getByTestId('schedule-time-start'), { target: { value: '08:00' } });
    fireEvent.change(screen.getByTestId('schedule-time-end'),   { target: { value: '20:00' } });
    fireEvent.change(screen.getByTestId('schedule-timezone'),   { target: { value: 'America/New_York' } });
    fireEvent.click(screen.getByTestId('day-6'));  // add Saturday

    fireEvent.click(screen.getByTestId('save-schedule-btn'));
    await waitFor(() => expect(api.updateCampaign).toHaveBeenCalledTimes(1));
    const [cid, payload] = api.updateCampaign.mock.calls[0];
    expect(cid).toBe('c1');
    expect(payload).toMatchObject({
      schedule_time_start: '08:00:00',
      schedule_time_end: '20:00:00',
      schedule_timezone: 'America/New_York',
      schedule_days: [1, 2, 3, 4, 5, 6],
      min_delay_seconds: 60,
    });
  });

  it('Schedule editor blocks save when start >= end', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, schedule_days: [1, 2, 3, 4, 5],
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(await screen.findByTestId('schedule-editor-toggle'));

    fireEvent.change(screen.getByTestId('schedule-time-start'), { target: { value: '18:00' } });
    fireEvent.change(screen.getByTestId('schedule-time-end'),   { target: { value: '09:00' } });

    expect(screen.getByTestId('schedule-validation-error').textContent)
      .toMatch(/start time must be before end time/i);
    expect(screen.getByTestId('save-schedule-btn')).toBeDisabled();
  });

  it('Apply-to-all calls applySignature when a saved signature exists', async () => {
    api.getCampaign.mockResolvedValue({ ...RUNNING_CAMPAIGN, signature: 'Saved sig' });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    await screen.findByTestId('signature-editor');
    const apply = screen.getByTestId('apply-signature-btn');
    expect(apply).not.toBeDisabled();  // saved sig, not dirty
    fireEvent.click(apply);
    await waitFor(() => expect(api.applySignature).toHaveBeenCalledWith('c1'));
  });

  it('editing a composed email saves via updateLeadEmail', async () => {
    api.listCampaignLeads.mockResolvedValue({
      items: [{
        id: 'L1', email: 'l@x.com', first_name: 'Jane', last_name: 'Doe',
        company: 'Acme', research_status: 'done', compose_status: 'done',
        send_status: 'pending',
      }],
      total: 1, page: 1, page_size: 50, total_pages: 1,
    });
    api.getLeadDetail.mockResolvedValue({
      composed_subject: 'Hi', composed_body: 'Body here',
      send_status: 'pending', research_data: { quality: 'low' },
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    fireEvent.click(await screen.findByText('View email'));
    fireEvent.click(await screen.findByTestId('edit-email-btn'));
    const editor = await screen.findByTestId('email-editor');
    // [0] = subject input, [1] = body textarea
    const bodyField = within(editor).getAllByRole('textbox')[1];
    fireEvent.change(bodyField, { target: { value: 'Body here\n\nAnthony\n555-1234' } });
    fireEvent.click(screen.getByTestId('save-email-btn'));
    await waitFor(() => expect(api.updateLeadEmail).toHaveBeenCalledWith(
      'c1', 'L1', expect.objectContaining({ composed_body: 'Body here\n\nAnthony\n555-1234' }),
    ));
  });

  it('previews the follow-up reply draft on demand', async () => {
    api.listCampaignLeads.mockResolvedValue({
      items: [{
        id: 'L1', email: 'l@x.com', first_name: 'Jane', last_name: 'Doe',
        company: 'Acme', research_status: 'done', compose_status: 'done',
        send_status: 'sent',
      }],
      total: 1, page: 1, page_size: 50, total_pages: 1,
    });
    api.getLeadDetail.mockResolvedValue({
      composed_subject: 'Hi', composed_body: 'Body here',
      send_status: 'sent', research_data: { quality: 'low' },
    });
    api.previewLeadReply.mockResolvedValue({
      node_id: 'n1', title: 'Bump', ai_compose: true, ai_prompt: 'nudge the trial',
      subject: 'Re: Hi', body: 'Just circling back, Jane.',
      regenerated_at_send: true, has_original_email: true,
      available_nodes: [{ node_id: 'n1', title: 'Bump', ai_compose: true, ai_prompt: 'nudge the trial' }],
    });

    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    fireEvent.click(await screen.findByText('View email'));
    fireEvent.click(await screen.findByTestId('preview-reply-btn'));

    await waitFor(() => expect(api.previewLeadReply).toHaveBeenCalledWith('c1', 'L1', undefined));
    expect((await screen.findByTestId('reply-preview-body')).textContent)
      .toContain('Just circling back, Jane.');
    expect(screen.getByTestId('reply-preview-subject').textContent).toContain('Re: Hi');
    // AI replies carry the regenerated-at-send caveat.
    expect(screen.getByTestId('reply-preview-ai-note')).toBeInTheDocument();
  });

  it('shows the goal-rewrite progress card while a rewrite is in flight', async () => {
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 100, composed: 95, sent: 50, failed: 0,
      composing: 5,
      goal_updated_at: '2026-06-15T22:57:48Z',
      rewrite_total: 50, rewrite_done: 30,
    });
    renderPage();
    const card = await screen.findByTestId('goal-rewrite-card');
    expect(within(card).getByTestId('rewrite-fraction')).toHaveTextContent('30 / 50');
    expect(within(card).getByTestId('rewrite-composing')).toHaveTextContent('5 currently being rewritten');
  });

  it('hides the rewrite card when the goal has never been edited', async () => {
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 100, composed: 100, sent: 50, failed: 0,
      composing: 0, goal_updated_at: null, rewrite_total: 0, rewrite_done: 0,
    });
    renderPage();
    // Wait for the pipeline card to render (composed counter is enough).
    await screen.findByTestId('campaign-name');
    expect(screen.queryByTestId('goal-rewrite-card')).not.toBeInTheDocument();
  });

  it('hides the rewrite card once recompose catches up', async () => {
    // Goal was edited but every lead has caught up — card stays out of the way.
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 100, composed: 100, sent: 50, failed: 0,
      composing: 0,
      goal_updated_at: '2026-06-15T22:57:48Z',
      rewrite_total: 50, rewrite_done: 50,
    });
    renderPage();
    await screen.findByTestId('campaign-name');
    expect(screen.queryByTestId('goal-rewrite-card')).not.toBeInTheDocument();
  });

  it('Stop button is greyed + disabled when nothing is researching/composing', async () => {
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 100, composed: 100, sent: 50, failed: 0,
      composing: 0, pipeline_active: false,
      goal_updated_at: null, rewrite_total: 0, rewrite_done: 0,
    });
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeDisabled());
    // Clicking a disabled button never opens the confirm modal.
    fireEvent.click(btn);
    expect(screen.queryByTestId('stop-confirm-modal')).not.toBeInTheDocument();
  });

  it('Stop button is enabled while research/compose is active', async () => {
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 60, composed: 40, sent: 0, failed: 0,
      composing: 5, pipeline_active: true,
      goal_updated_at: null, rewrite_total: 0, rewrite_done: 0,
    });
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeEnabled());
  });

  it('Stop button opens a confirm modal that explains what will be halted', async () => {
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeEnabled());
    fireEvent.click(btn);
    const modal = await screen.findByTestId('stop-confirm-modal');
    expect(within(modal).getByText(/Anthropic \+ Apollo \+ Hunter/)).toBeInTheDocument();
    expect(within(modal).getByTestId('stop-confirm-btn')).toHaveTextContent('Yes, stop it');
    expect(within(modal).getByTestId('stop-cancel-btn')).toHaveTextContent('Cancel');
  });

  it('Confirming Stop calls the API and shows a per-stage success toast', async () => {
    api.stopCampaignPipeline.mockResolvedValue({
      campaign_id: 'c1',
      terminated: 8,
      terminated_by_kind: { research: 5, compose: 3, send: 0 },
      purged_queued: 0,
      purged_unacked: 42,
    });
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeEnabled());
    fireEvent.click(btn);
    fireEvent.click(await screen.findByTestId('stop-confirm-btn'));
    await waitFor(() => expect(api.stopCampaignPipeline).toHaveBeenCalledWith('c1'));
    expect(await screen.findByText(/Killed 5 research \+ 3 compose/)).toBeInTheDocument();
    expect(screen.getByText(/purged 42 deferred/)).toBeInTheDocument();
  });

  it('Cancel closes the confirm modal without calling the API', async () => {
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeEnabled());
    fireEvent.click(btn);
    fireEvent.click(await screen.findByTestId('stop-cancel-btn'));
    await waitFor(() => expect(screen.queryByTestId('stop-confirm-modal')).not.toBeInTheDocument());
    expect(api.stopCampaignPipeline).not.toHaveBeenCalled();
  });

  it('Renders a "Stopped by you" banner instead of the deliverability breaker banner', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'paused',
      auto_pause_reason: 'user_stopped',
    });
    renderPage();
    expect(await screen.findByTestId('user-stopped-banner')).toHaveTextContent('Stopped by you');
    expect(screen.queryByTestId('breaker-banner')).not.toBeInTheDocument();
  });

  it('Still shows the deliverability breaker banner when the reason is not user_stopped', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'paused',
      auto_pause_reason: 'bounce rate 8% exceeded 5% threshold',
    });
    renderPage();
    expect(await screen.findByTestId('breaker-banner')).toHaveTextContent('deliverability breaker');
    expect(screen.queryByTestId('user-stopped-banner')).not.toBeInTheDocument();
  });

  it('Stop button is disabled when pipeline_active is false', async () => {
    api.getPreviewProgress.mockResolvedValue({
      total_leads: 100, researched: 100, composed: 100, sent: 50, failed: 0,
      composing: 0, pipeline_active: false,
      goal_updated_at: null, rewrite_total: 0, rewrite_done: 0,
    });
    renderPage();
    const btn = await screen.findByTestId('stop-pipeline-button');
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveAttribute(
      'title', expect.stringContaining('Nothing to stop'),
    );
  });
});

describe('Deliverability guard', () => {
  it('renders the deliverability strip with rates + domain headroom', async () => {
    renderPage();
    const strip = await screen.findByTestId('deliverability-strip');
    expect(within(strip).getByTestId('deliv-bounce-rate')).toHaveTextContent('2.5%');
    const headroom = within(strip).getByTestId('domain-headroom');
    expect(headroom).toHaveTextContent('x.com');
    expect(headroom).toHaveTextContent('88/100 this hour');
    expect(headroom).toHaveTextContent('460/500 today');
  });

  it('shows the breaker banner with the reason on an auto-paused campaign', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN,
      status: 'paused',
      auto_paused_at: '2026-06-12T10:00:00Z',
      auto_pause_reason: 'Hard-bounce rate 10.0% over the last 24h (4/40 sends) crossed the 5% threshold',
    });
    renderPage();
    const banner = await screen.findByTestId('breaker-banner');
    expect(banner).toHaveTextContent(/deliverability breaker/i);
    expect(banner).toHaveTextContent(/10\.0%/);
    expect(banner).toHaveTextContent(/will not resume on its own/i);
    // Manual resume stays available right above the banner.
    expect(screen.getByTestId('pause-resume-button')).toHaveTextContent('Resume');
  });

  it('saving the send-time-optimization toggle includes it in the PATCH', async () => {
    api.updateCampaign.mockResolvedValue({});
    renderPage();
    await screen.findByTestId('schedule-editor');
    fireEvent.click(screen.getByTestId('schedule-editor-toggle'));
    const toggle = screen.getByTestId('send-time-optimization-toggle');
    fireEvent.click(within(toggle).getByRole('checkbox'));
    fireEvent.click(screen.getByTestId('save-schedule-btn'));
    await waitFor(() => {
      expect(api.updateCampaign).toHaveBeenCalled();
      const payload = api.updateCampaign.mock.calls[0][1];
      expect(payload.send_time_optimization).toBe(true);
    });
  });
});

describe("What's working panel", () => {
  it('hidden when there are no reply outcomes yet', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.queryByTestId('whats-working-panel')).toBeNull();
  });

  it('renders counts, angle sections, and winning examples when data exists', async () => {
    api.getCopyInsights.mockResolvedValue({
      outcome_counts: { positive: 3, neutral: 1, negative: 2 },
      insights: {
        winning_openers: ['lead with their tech stack'],
        subject_patterns: ['short, lowercase'],
        value_framings: [], cta_styles: [], avoid: ['jargon walls'],
      },
      refreshed_at: '2026-06-12T10:00:00Z',
      winning_examples: [{ subject: 'quick one', body: 'The winning body.' }],
    });
    renderPage();
    const panel = await screen.findByTestId('whats-working-panel');
    expect(within(panel).getByTestId('outcome-counts')).toHaveTextContent('3 positive');
    expect(panel).toHaveTextContent('lead with their tech stack');
    expect(panel).toHaveTextContent('jargon walls');
    expect(within(panel).getByTestId('winning-examples')).toHaveTextContent('quick one');
  });
});

describe('SenderEditor', () => {
  it('warns + blocks launch when sender is a placeholder, and lets you set a real one', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'draft',
      sender_name: 'Operator', sender_email: 'you@example.com', sender_ready: false,
    });
    api.updateCampaign.mockResolvedValue({});
    renderPage();

    // Not-ready warning shown.
    expect(await screen.findByTestId('sender-not-ready')).toBeInTheDocument();

    // Edit → fix the email → save calls updateCampaign with the new sender.
    fireEvent.click(screen.getByTestId('edit-sender-btn'));
    const emailInput = screen.getByTestId('sender-email-input');
    fireEvent.change(emailInput, { target: { value: 'real@org.com' } });
    fireEvent.click(screen.getByTestId('save-sender-btn'));
    await waitFor(() =>
      expect(api.updateCampaign).toHaveBeenCalledWith('c1', expect.objectContaining({
        sender_email: 'real@org.com', sender_name: 'Operator',
      })));
  });

  it('disables save on an invalid email', async () => {
    api.getCampaign.mockResolvedValue({
      ...RUNNING_CAMPAIGN, status: 'draft',
      sender_name: 'Operator', sender_email: 'you@example.com', sender_ready: false,
    });
    renderPage();
    fireEvent.click(await screen.findByTestId('edit-sender-btn'));
    fireEvent.change(screen.getByTestId('sender-email-input'), { target: { value: 'nope' } });
    expect(screen.getByTestId('sender-email-error')).toBeInTheDocument();
    expect(screen.getByTestId('save-sender-btn')).toBeDisabled();
  });
});

describe('Lead recipient editing + find contact', () => {
  const LEADS_ONE = {
    items: [{
      id: 'L1', email: null, first_name: null, last_name: null,
      company: 'Helpful NP', research_status: 'done', compose_status: 'done',
      send_status: 'pending',
    }],
    total: 1, page: 1, page_size: 50, total_pages: 1,
  };

  it('sets a recipient by hand via updateLeadEmail', async () => {
    api.listCampaignLeads.mockResolvedValue(LEADS_ONE);
    api.getLeadDetail.mockResolvedValue({
      email: null, composed_subject: 'Hi', composed_body: 'Hi there,',
      send_status: 'pending', research_data: { quality: 'low' },
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    fireEvent.click(await screen.findByText('View email'));

    expect(await screen.findByTestId('recipient-value')).toHaveTextContent(/no recipient yet/i);
    fireEvent.click(screen.getByTestId('edit-recipient-btn'));
    const input = screen.getByTestId('recipient-input');
    // Invalid → save disabled.
    fireEvent.change(input, { target: { value: 'nope' } });
    expect(screen.getByTestId('recipient-error')).toBeInTheDocument();
    expect(screen.getByTestId('save-recipient-btn')).toBeDisabled();
    // Valid → saves.
    fireEvent.change(input, { target: { value: 'dev@org.org' } });
    fireEvent.click(screen.getByTestId('save-recipient-btn'));
    await waitFor(() => expect(api.updateLeadEmail).toHaveBeenCalledWith(
      'c1', 'L1', { email: 'dev@org.org' }));
  });

  it('offers Find contact for intent leads and calls the API', async () => {
    api.listCampaignLeads.mockResolvedValue(LEADS_ONE);
    api.getLeadDetail.mockResolvedValue({
      email: null, composed_subject: 'Hi', composed_body: 'Hi there,',
      send_status: 'pending',
      research_data: { quality: 'low', from_intent_engine: true, intent_org_id: 'o1' },
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    fireEvent.click(await screen.findByText('View email'));

    const siteInput = await screen.findByTestId('find-contact-website');
    fireEvent.change(siteInput, { target: { value: 'https://helpful.org' } });
    fireEvent.click(screen.getByTestId('find-contact-btn'));
    await waitFor(() => expect(api.findLeadContact).toHaveBeenCalledWith(
      'c1', 'L1', { website: 'https://helpful.org' }));
  });

  it('hides Find contact for non-intent leads', async () => {
    api.listCampaignLeads.mockResolvedValue(LEADS_ONE);
    api.getLeadDetail.mockResolvedValue({
      email: null, composed_subject: 'Hi', composed_body: 'Hi there,',
      send_status: 'pending', research_data: { quality: 'low' },
    });
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('tab-leads'));
    fireEvent.click(await screen.findByText('View email'));
    await screen.findByTestId('recipient-section');
    expect(screen.queryByTestId('find-contact-btn')).not.toBeInTheDocument();
  });
});

describe('Retarget engaged leads', () => {
  it('opens the retarget modal, shows engaged count, and creates a retarget campaign', async () => {
    renderPage();
    await screen.findByTestId('overview-tab');
    fireEvent.click(screen.getByTestId('retarget-button'));

    const count = await screen.findByTestId('retarget-engaged-count');
    expect(count).toHaveTextContent('12');
    fireEvent.click(screen.getByTestId('retarget-go'));
    await waitFor(() => expect(api.retargetCampaign).toHaveBeenCalledWith('c1', {}));
  });
});
