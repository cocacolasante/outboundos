import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/socialRadar.js', () => ({
  listSearches: vi.fn(),
  getSearch: vi.fn(),
  createSearch: vi.fn(),
  updateSearch: vi.fn(),
  deleteSearch: vi.fn(),
  runSearch: vi.fn(),
  cleanupStalePosts: vi.fn(),
  previewExpand: vi.fn(),
  previewExpandForSearch: vi.fn(),
  listOpportunities: vi.fn(),
  updateOpportunity: vi.fn(),
}));

import * as api from '../api/socialRadar.js';
import SocialRadar, {
  classifyWatchlistEntries,
  parseWatchlistEntries,
} from './SocialRadar.jsx';
import { ToastProvider } from '../components/Toast.jsx';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <SocialRadar />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function paged(items, overrides = {}) {
  return {
    items, total: items.length, page: 1, page_size: 25, total_pages: items.length ? 1 : 0,
    ...overrides,
  };
}

const SEARCH_FIXTURE = {
  id: 's1', name: 'MSP intent', topic: 'frustrated with our IT provider',
  niche: 'mid-market', geography: 'NE US',
  source: 'linkedin', frequency: 'manual', status: 'active',
  last_run_at: '2026-05-30T12:00:00Z', next_run_at: null, last_run_status: 'done',
  last_run_error: null, expanded_query_count: 18,
  post_count: 12, opportunity_count: 8,
  max_post_age_days: 30,
  created_at: '2026-05-29T12:00:00Z', updated_at: '2026-05-30T12:00:00Z',
};

const OPP_FIXTURE = {
  id: 'o1', post_id: 'p1', search_id: 's1', search_name: 'MSP intent',
  score: 8, category: 'msp', buying_signal: true,
  pain_summary: 'IT provider unresponsive.',
  qualification_reason: 'Decision-maker, clear pain.',
  suggested_comment: 'Sounds painful — what SLA does your team need?',
  suggested_connection_request: 'Saw your post on MSP frustrations — open to swapping notes?',
  suggested_follow_up: 'Thanks for connecting. Quick context — I work with...',
  recommended_action: 'comment',
  status: 'new', notes: null,
  created_at: '2026-05-30T13:00:00Z', updated_at: '2026-05-30T13:00:00Z',
  post: {
    id: 'p1', provider: 'linkedin',
    post_url: 'https://www.linkedin.com/posts/jane-activity-1',
    author_name: 'Jane Doe', author_profile_url: 'https://linkedin.com/in/jane',
    author_headline: 'CFO at Acme', company_name: 'Acme',
    post_text: 'Our MSP is impossible to reach when something breaks.',
    post_date: null, discovered_at: '2026-05-30T13:00:00Z',
    discovered_via: 'frustrated with our msp',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  api.listSearches.mockResolvedValue(paged([SEARCH_FIXTURE]));
  api.listOpportunities.mockResolvedValue(paged([OPP_FIXTURE]));
});


describe('SocialRadar — Feed tab', () => {
  it('renders an opportunity card with score, copy buttons, and post text', async () => {
    renderPage();
    await screen.findByTestId('feed-tab');
    const card = await screen.findByTestId(`opportunity-${OPP_FIXTURE.id}`);
    expect(within(card).getByTestId('score-badge').textContent).toBe('8');
    expect(within(card).getByText(/Jane Doe/)).toBeInTheDocument();
    expect(within(card).getByText(/CFO at Acme/)).toBeInTheDocument();
    expect(within(card).getByTestId('post-text').textContent).toMatch(/MSP is impossible/);
    expect(within(card).getByTestId(`copy-comment-${OPP_FIXTURE.id}`)).toBeInTheDocument();
    expect(within(card).getByTestId(`copy-connect-${OPP_FIXTURE.id}`)).toBeInTheDocument();
  });

  it('renders an empty-state when no opportunities are returned', async () => {
    api.listOpportunities.mockResolvedValue(paged([]));
    renderPage();
    await waitFor(() => expect(screen.getByTestId('feed-empty')).toBeInTheDocument());
  });

  it('changes status via the per-row dropdown', async () => {
    api.updateOpportunity.mockResolvedValue({ ...OPP_FIXTURE, status: 'saved' });
    const user = userEvent.setup();
    renderPage();
    const select = await screen.findByTestId(`opp-status-${OPP_FIXTURE.id}`);
    await user.selectOptions(select, 'saved');
    await waitFor(() => {
      expect(api.updateOpportunity).toHaveBeenCalledWith('o1', { status: 'saved' });
    });
  });

  it('passes filters to the API', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('feed-tab');
    // Wait for the searches dropdown to hydrate so the option exists.
    await screen.findByRole('option', { name: 'MSP intent' });

    await user.selectOptions(screen.getByLabelText(/Filter by search/i), 's1');
    await user.selectOptions(screen.getByLabelText(/Filter by category/i), 'msp');
    await user.selectOptions(screen.getByLabelText(/Filter by status/i), 'saved');
    await user.type(screen.getByTestId('min-score-filter'), '7');

    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.search_id).toBe('s1');
      expect(last.category).toBe('msp');
      expect(last.status).toBe('saved');
      expect(last.min_score).toBe(7);
    });
  });

  it('Source filter drives the API source query param', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('feed-tab');

    await user.selectOptions(screen.getByTestId('source-filter'), 'reddit');
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.source).toBe('reddit');
    });

    // Switch back to All sources → param drops out (undefined, not 'all').
    await user.selectOptions(screen.getByTestId('source-filter'), '');
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.source).toBeUndefined();
    });
  });

  it('Sort-by + sort-order land in the API params', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('feed-tab');

    // Initial call uses the defaults.
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.sort_by).toBe('score');
      expect(last.sort_order).toBe('desc');
    });

    // Change sort key.
    await user.selectOptions(screen.getByTestId('sort-by'), 'discovered_at');
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.sort_by).toBe('discovered_at');
    });

    // Flip the order.
    await user.click(screen.getByTestId('sort-order'));
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.sort_order).toBe('asc');
    });
    expect(screen.getByTestId('sort-order').textContent).toMatch(/asc/i);

    // Flip back.
    await user.click(screen.getByTestId('sort-order'));
    await waitFor(() => {
      const last = api.listOpportunities.mock.calls.at(-1)[0];
      expect(last.sort_order).toBe('desc');
    });
  });
});


describe('SocialRadar — Searches tab', () => {
  it('lists existing searches with post/opp counts + Run now', async () => {
    api.runSearch.mockResolvedValue({ status: 'queued', task_id: 't1' });
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('tab-searches'));
    const row = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    expect(within(row).getByText('MSP intent')).toBeInTheDocument();
    expect(within(row).getByText('12')).toBeInTheDocument();  // post_count
    expect(within(row).getByText('8')).toBeInTheDocument();   // opportunity_count

    await user.click(within(row).getByTestId(`run-${SEARCH_FIXTURE.id}`));
    await waitFor(() => expect(api.runSearch).toHaveBeenCalledWith('s1'));
  });

  it('shows an empty-state when no searches exist', async () => {
    api.listSearches.mockResolvedValue(paged([]));
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    expect(await screen.findByTestId('searches-empty')).toBeInTheDocument();
  });
});


describe('SocialRadar — Editor modal', () => {
  it('opens via "+ New search", previews queries, then saves via createSearch', async () => {
    api.previewExpand.mockResolvedValue({
      queries: ['frustrated with our msp', 'looking for a new phone system'],
    });
    api.createSearch.mockResolvedValue({
      ...SEARCH_FIXTURE, id: 'new', name: 'Brand new',
    });

    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('new-search-btn'));
    const modal = await screen.findByTestId('search-editor-modal');

    await user.type(within(modal).getByTestId('input-name'), 'Brand new');
    await user.type(within(modal).getByTestId('input-topic'), 'frustrated with technology');
    await user.type(within(modal).getByTestId('input-niche'), 'mid-market');

    // Preview-queries flow
    await user.click(within(modal).getByTestId('preview-queries-btn'));
    await waitFor(() => expect(api.previewExpand).toHaveBeenCalledTimes(1));
    const chips = await within(modal).findByTestId('preview-chips');
    expect(within(chips).getByText('frustrated with our msp')).toBeInTheDocument();
    expect(within(chips).getByText('looking for a new phone system')).toBeInTheDocument();

    // Save
    await user.click(within(modal).getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.createSearch).toHaveBeenCalledTimes(1);
      const payload = api.createSearch.mock.calls[0][0];
      expect(payload.name).toBe('Brand new');
      expect(payload.topic).toBe('frustrated with technology');
      expect(payload.niche).toBe('mid-market');
      // Default lookback window is 30 days.
      expect(payload.max_post_age_days).toBe(30);
    });
  });

  it('Lookback window is editable in the modal and lands in the payload', async () => {
    api.createSearch.mockResolvedValue({ ...SEARCH_FIXTURE, id: 'new', max_post_age_days: 90 });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));

    await user.type(screen.getByTestId('input-name'), 'Trend research');
    await user.type(screen.getByTestId('input-topic'), 'cloud cost frustrations');

    const lookback = screen.getByTestId('input-max-post-age-days');
    expect(lookback).toHaveValue(30);  // default

    await user.clear(lookback);
    await user.type(lookback, '90');

    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.createSearch).toHaveBeenCalledTimes(1);
      expect(api.createSearch.mock.calls[0][0].max_post_age_days).toBe(90);
    });
  });

  it('LinkedIn cross-link checkbox defaults to checked and lands in the create payload', async () => {
    api.createSearch.mockResolvedValue({ ...SEARCH_FIXTURE, id: 'new' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));

    const crosslink = await screen.findByTestId('input-linkedin-crosslink');
    // Default ON — free signal, no extra cost.
    expect(crosslink).toBeChecked();

    await user.type(screen.getByTestId('input-name'), 'X');
    await user.type(screen.getByTestId('input-topic'), 'Y');
    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.createSearch).toHaveBeenCalledTimes(1);
      expect(api.createSearch.mock.calls[0][0].linkedin_crosslink_enabled).toBe(true);
    });
  });

  it('Unchecking LinkedIn cross-link sends linkedin_crosslink_enabled=false', async () => {
    api.createSearch.mockResolvedValue({ ...SEARCH_FIXTURE, id: 'new' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));

    const crosslink = await screen.findByTestId('input-linkedin-crosslink');
    await user.click(crosslink);
    expect(crosslink).not.toBeChecked();

    await user.type(screen.getByTestId('input-name'), 'X');
    await user.type(screen.getByTestId('input-topic'), 'Y');
    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.createSearch).toHaveBeenCalledTimes(1);
      expect(api.createSearch.mock.calls[0][0].linkedin_crosslink_enabled).toBe(false);
    });
  });

  it('save button is disabled until both name and topic are filled in', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));
    const btn = await screen.findByTestId('save-search-btn');
    expect(btn).toBeDisabled();

    await user.type(screen.getByTestId('input-name'), 'X');
    expect(btn).toBeDisabled();

    await user.type(screen.getByTestId('input-topic'), 'Y');
    expect(btn).not.toBeDisabled();
  });

  it('opens edit modal via the per-row Edit button and calls updateSearch on save', async () => {
    api.updateSearch.mockResolvedValue({ ...SEARCH_FIXTURE, name: 'Renamed' });
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('tab-searches'));
    const row = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    await user.click(within(row).getByTestId(`edit-${SEARCH_FIXTURE.id}`));
    const modal = await screen.findByTestId('search-editor-modal');

    const nameInput = within(modal).getByTestId('input-name');
    await user.clear(nameInput);
    await user.type(nameInput, 'Renamed');

    await user.click(within(modal).getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.updateSearch).toHaveBeenCalledWith('s1', expect.objectContaining({ name: 'Renamed' }));
    });
  });

  it('saving a checkbox toggle persists and the next open reads the new value (no stale cache)', async () => {
    // Regression: toggling ``linkedin_web_search_enabled`` from false → true,
    // saving, and reopening the editor must show the new value.  The bug
    // was that ``invalidateQueries`` alone left the stale cached body
    // visible on reopen — the modal's one-shot hydration latched onto
    // the stale value and never re-read.  Fix seeds the cache with the
    // PATCH response so reopen reads fresh.

    // First open: detail says the toggle is OFF.
    api.getSearch.mockResolvedValueOnce({
      ...SEARCH_FIXTURE,
      linkedin_web_search_enabled: false,
      linkedin_crosslink_enabled: true,
      tone: 'helpful',
      sender_name: null,
      include_keywords: [],
      exclude_keywords: [],
      expanded_queries: [],
      linkedin_profile_watchlist: [],
      max_run_cost_usd: 1.0,
      max_queries_per_run: 20,
      max_posts_per_query: 30,
      max_qualified_per_run: 100,
    });
    // PATCH echoes the new state (this is what the backend already does
    // via response_model=SocialListeningSearchResponse).
    api.updateSearch.mockResolvedValue({
      ...SEARCH_FIXTURE,
      linkedin_web_search_enabled: true,
      linkedin_crosslink_enabled: true,
      tone: 'helpful',
      sender_name: null,
      include_keywords: [],
      exclude_keywords: [],
      expanded_queries: [],
      linkedin_profile_watchlist: [],
      max_run_cost_usd: 1.0,
      max_queries_per_run: 20,
      max_posts_per_query: 30,
      max_qualified_per_run: 100,
    });

    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    const row = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    await user.click(within(row).getByTestId(`edit-${SEARCH_FIXTURE.id}`));

    let modal = await screen.findByTestId('search-editor-modal');
    const checkbox1 = within(modal).getByTestId('input-linkedin-web-search');
    // Wait for the detail fetch + hydration.
    await waitFor(() => expect(checkbox1).not.toBeChecked());

    // Toggle ON, then save.
    await user.click(checkbox1);
    await waitFor(() => expect(checkbox1).toBeChecked());
    await user.click(within(modal).getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.updateSearch).toHaveBeenCalledTimes(1);
      expect(api.updateSearch.mock.calls[0][1].linkedin_web_search_enabled).toBe(true);
    });
    // Modal closes on success.
    await waitFor(() =>
      expect(screen.queryByTestId('search-editor-modal')).toBeNull(),
    );

    // REOPEN.  If the cache is stale, the checkbox will render unchecked
    // and stay that way (the one-shot hydration latches onto the cached
    // false).  With the fix, the cache was seeded with the PATCH
    // response, so the checkbox renders CHECKED.
    const row2 = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    await user.click(within(row2).getByTestId(`edit-${SEARCH_FIXTURE.id}`));
    modal = await screen.findByTestId('search-editor-modal');
    const checkbox2 = within(modal).getByTestId('input-linkedin-web-search');
    await waitFor(() => expect(checkbox2).toBeChecked());
  });

  it('Edit from the searches table pre-fills full-detail fields (tone, sender_name, caps, expanded_queries)', async () => {
    // The searches table only passes the SUMMARY shape (no tone, no caps,
    // no expanded_queries).  Modal should fetch full detail so those
    // fields aren't blank.
    api.getSearch.mockResolvedValue({
      ...SEARCH_FIXTURE,
      tone: 'warm and direct',
      sender_name: 'Anthony C.',
      max_post_age_days: 14,
      max_queries_per_run: 12,
      include_keywords: ['msp', 'ucaas'],
      expanded_queries: ['frustrated with our msp', 'looking for a new phone provider'],
      linkedin_profile_watchlist: ['https://www.linkedin.com/in/jane-doe'],
    });

    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    const row = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    await user.click(within(row).getByTestId(`edit-${SEARCH_FIXTURE.id}`));
    const modal = await screen.findByTestId('search-editor-modal');

    // Wait for the modal to fetch full detail and re-hydrate the draft.
    await waitFor(() => {
      expect(within(modal).getByTestId('input-name')).toHaveValue('MSP intent');
      expect(within(modal).getByTestId('input-sender-name')).toHaveValue('Anthony C.');
      expect(within(modal).getByTestId('input-max-post-age-days')).toHaveValue(14);
      // include_keywords are a CSV of the array.
      expect(within(modal).getByTestId('input-include').value)
        .toMatch(/msp.*ucaas/);
      // expanded_queries appear one-per-line in the textarea.
      expect(within(modal).getByTestId('input-expanded-queries').value)
        .toContain('frustrated with our msp');
      // watchlist URL pre-filled.
      expect(within(modal).getByTestId('input-watchlist').value)
        .toContain('jane-doe');
    });
  });
});


describe('SocialRadar — Search detail view', () => {
  const DETAIL_FIXTURE = {
    ...SEARCH_FIXTURE,
    expanded_queries: ['frustrated with msp', 'looking for new it', 'msp pain'],
    include_keywords: ['msp'],
    exclude_keywords: [],
    tone: 'helpful',
    sender_name: 'Anthony',
    max_queries_per_run: 20,
    max_posts_per_query: 30,
    max_qualified_per_run: 100,
  };

  beforeEach(() => {
    api.getSearch.mockResolvedValue(DETAIL_FIXTURE);
  });

  it('clicking the row name opens the search detail view (not the edit modal)', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('tab-searches'));
    const row = await screen.findByTestId(`search-row-${SEARCH_FIXTURE.id}`);
    await user.click(within(row).getByTestId(`view-${SEARCH_FIXTURE.id}`));

    expect(await screen.findByTestId('search-detail')).toBeInTheDocument();
    // The edit modal should NOT have opened.
    expect(screen.queryByTestId('search-editor-modal')).toBeNull();
    await waitFor(() => expect(api.getSearch).toHaveBeenCalledWith('s1'));
  });

  it('renders activity card with last run + counts + expanded queries', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    const activity = await screen.findByTestId('detail-activity');
    expect(within(activity).getByText('Posts found').nextSibling?.textContent
      || within(activity).getByText('12')).toBeTruthy();
    expect(within(activity).getByText('done')).toBeInTheDocument();

    const chips = await screen.findByTestId('detail-expanded-queries');
    expect(within(chips).getByText('frustrated with msp')).toBeInTheDocument();
    expect(within(chips).getByText('looking for new it')).toBeInTheDocument();
    expect(within(chips).getByText('msp pain')).toBeInTheDocument();
  });

  it('renders an opportunity card for each opp this search found', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    await screen.findByTestId('detail-opps-list');
    expect(screen.getByTestId(`opportunity-${OPP_FIXTURE.id}`)).toBeInTheDocument();

    // The list query filters by search_id.
    await waitFor(() => {
      const calls = api.listOpportunities.mock.calls;
      const detailCall = calls.find((c) => c[0]?.search_id === 's1');
      expect(detailCall).toBeTruthy();
    });
  });

  it('shows an empty-state when this search has no opportunities yet', async () => {
    api.listOpportunities.mockImplementation((params) => {
      if (params?.search_id === 's1') return Promise.resolve(paged([]));
      return Promise.resolve(paged([OPP_FIXTURE]));
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    expect(await screen.findByTestId('detail-empty')).toBeInTheDocument();
  });

  it('surfaces the last_run_error when one is set', async () => {
    api.getSearch.mockResolvedValue({
      ...DETAIL_FIXTURE,
      last_run_status: 'failed',
      last_run_error: 'Anthropic 503',
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    const err = await screen.findByTestId('detail-last-error');
    expect(err.textContent).toMatch(/Anthropic 503/);
  });

  it('Back returns to the Searches tab', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    await user.click(await screen.findByTestId('detail-back-btn'));
    expect(await screen.findByTestId('searches-tab')).toBeInTheDocument();
    expect(screen.queryByTestId('search-detail')).toBeNull();
  });

  it('Edit button on detail opens the modal pre-filled', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    await user.click(await screen.findByTestId('detail-edit-btn'));
    const modal = await screen.findByTestId('search-editor-modal');
    expect(within(modal).getByTestId('input-name').value).toBe('MSP intent');
  });

  it('detail view header includes the configured lookback window', async () => {
    api.getSearch.mockResolvedValue({ ...DETAIL_FIXTURE, max_post_age_days: 7 });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    const lookback = await screen.findByTestId('detail-lookback');
    expect(lookback.textContent).toBe('7 days');
  });

  it('Editor watchlist textarea saves profile URLs to linkedin_profile_watchlist', async () => {
    api.getSearch.mockResolvedValue({
      ...DETAIL_FIXTURE,
      sources: ['linkedin', 'reddit', 'twitter'],
      linkedin_profile_watchlist: [],
    });
    api.updateSearch.mockResolvedValue({ ...DETAIL_FIXTURE });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    await user.click(await screen.findByTestId('detail-edit-btn'));

    const wl = await screen.findByTestId('input-watchlist');
    await user.type(wl,
      'https://www.linkedin.com/in/jane-doe\nhttps://www.linkedin.com/in/bob-smith',
    );

    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      const [, payload] = api.updateSearch.mock.calls[0];
      expect(payload.linkedin_profile_watchlist).toEqual([
        'https://www.linkedin.com/in/jane-doe',
        'https://www.linkedin.com/in/bob-smith',
      ]);
    });
  });

  it('detail header shows watchlist count when non-empty', async () => {
    api.getSearch.mockResolvedValue({
      ...DETAIL_FIXTURE,
      linkedin_profile_watchlist: [
        'https://www.linkedin.com/in/a',
        'https://www.linkedin.com/in/b',
        'https://www.linkedin.com/in/c',
      ],
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    const count = await screen.findByTestId('detail-watchlist-count');
    expect(count.textContent).toMatch(/3 profiles/);
  });

  it('detail header shows the configured sources list', async () => {
    api.getSearch.mockResolvedValue({ ...DETAIL_FIXTURE, sources: ['linkedin', 'reddit', 'twitter'] });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    const sources = await screen.findByTestId('detail-sources');
    expect(sources.textContent).toMatch(/linkedin/);
    expect(sources.textContent).toMatch(/reddit/);
    expect(sources.textContent).toMatch(/twitter/);
  });

  it('Editor modal source picker toggles sources and the save payload reflects them', async () => {
    api.getSearch.mockResolvedValue({ ...DETAIL_FIXTURE, sources: ['linkedin', 'reddit', 'twitter'] });
    api.updateSearch.mockResolvedValue({ ...DETAIL_FIXTURE });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    await user.click(await screen.findByTestId('detail-edit-btn'));

    // All three sources start active.
    const linkedin = await screen.findByTestId('source-linkedin');
    expect(linkedin).toHaveAttribute('aria-checked', 'true');

    // Toggle LinkedIn off (low-recall, user might want Reddit+Twitter only).
    await user.click(linkedin);
    expect(linkedin).toHaveAttribute('aria-checked', 'false');

    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.updateSearch).toHaveBeenCalledTimes(1);
      const [, payload] = api.updateSearch.mock.calls[0];
      expect(payload.sources.sort()).toEqual(['reddit', 'twitter']);
    });
  });

  it('detail Activity renders per-query stats when last_run_stats has data', async () => {
    api.getSearch.mockResolvedValue({
      ...DETAIL_FIXTURE,
      last_run_stats: {
        queries: [
          { query: 'frustrated with our msp', raw: 5, kept: 3, dropped_stale: 1, dropped_undated: 1, upserted_new: 2 },
          { query: 'looking for new internet', raw: 0, kept: 0 },
        ],
        summary: { total_queries: 2, total_kept: 3, total_dropped_stale: 1, total_dropped_undated: 1 },
      },
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    const panel = await screen.findByTestId('detail-perquery-stats');
    expect(within(panel).getByText(/3 kept/)).toBeInTheDocument();

    // Expand the per-query table.
    await user.click(within(panel).getByTestId('perquery-stats-toggle'));
    expect(within(panel).getByText('frustrated with our msp')).toBeInTheDocument();
    expect(within(panel).getByText('looking for new internet')).toBeInTheDocument();
  });

  it('Edit modal exposes the expanded_queries textarea pre-filled', async () => {
    api.getSearch.mockResolvedValue({
      ...DETAIL_FIXTURE,
      expanded_queries: ['frustrated with our msp', 'looking for new internet provider'],
    });
    api.updateSearch.mockResolvedValue({ ...DETAIL_FIXTURE });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
    await user.click(await screen.findByTestId('detail-edit-btn'));

    const ta = await screen.findByTestId('input-expanded-queries');
    expect(ta.value).toContain('frustrated with our msp');
    expect(ta.value).toContain('looking for new internet provider');

    // Edit: remove the second line, add a new one.
    await user.clear(ta);
    await user.type(ta, 'frustrated with our msp\nanyone else fed up with comcast');

    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.updateSearch).toHaveBeenCalledTimes(1);
      const [, payload] = api.updateSearch.mock.calls[0];
      expect(payload.expanded_queries).toEqual([
        'frustrated with our msp',
        'anyone else fed up with comcast',
      ]);
    });
  });

  it('Clean up stale button fires cleanupStalePosts after confirm', async () => {
    api.cleanupStalePosts.mockResolvedValue({ deleted: 42 });
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      renderPage();
      await user.click(screen.getByTestId('tab-searches'));
      await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));
      await user.click(await screen.findByTestId('detail-cleanup-btn'));
      await waitFor(() => expect(api.cleanupStalePosts).toHaveBeenCalledWith('s1'));
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('Run now on detail calls runSearch', async () => {
    api.runSearch.mockResolvedValue({ status: 'queued', task_id: 't1' });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('tab-searches'));
    await user.click(await screen.findByTestId(`view-${SEARCH_FIXTURE.id}`));

    await user.click(await screen.findByTestId('detail-run-btn'));
    await waitFor(() => expect(api.runSearch).toHaveBeenCalledWith('s1'));
  });
});


describe('classifyWatchlistEntries — bulk-paste validator', () => {
  it('parses newline-separated LinkedIn URLs', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe\nhttps://www.linkedin.com/in/bob-smith',
    );
    expect(valid).toHaveLength(2);
    expect(invalid).toHaveLength(0);
  });

  it('parses comma-separated LinkedIn URLs (CSV-column paste)', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane, https://www.linkedin.com/in/bob, https://www.linkedin.com/in/carol',
    );
    expect(valid).toHaveLength(3);
    expect(invalid).toHaveLength(0);
  });

  it('accepts any whitespace (spreadsheet-row paste with tabs)', () => {
    const text = 'https://www.linkedin.com/in/a\thttps://www.linkedin.com/in/b\nhttps://www.linkedin.com/in/c';
    const { valid } = classifyWatchlistEntries(text);
    expect(valid).toHaveLength(3);
  });

  it('flags non-LinkedIn URLs as invalid', () => {
    // ``random text`` is two whitespace-separated tokens (the splitter is
    // intentionally permissive — that's what makes spreadsheet-cell paste
    // work).  So 1 valid (jane) + 4 invalid (twitter, random, text,
    // /company/foo).
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane\nhttps://twitter.com/jane\nrandom text\nhttps://www.linkedin.com/company/foo',
    );
    expect(valid).toHaveLength(1);
    expect(invalid).toHaveLength(4);
  });

  it('dedupes valid URLs by slug regardless of casing or trailing slash', () => {
    const { valid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe\n' +
      'https://www.linkedin.com/in/Jane-Doe/\n' +
      'https://www.linkedin.com/in/jane-doe?utm=share',
    );
    // Three flavours of the same slug — kept as one.
    expect(valid).toHaveLength(1);
  });

  it('empty input → empty arrays', () => {
    expect(classifyWatchlistEntries('')).toEqual({ valid: [], invalid: [] });
    expect(classifyWatchlistEntries('   \n   ')).toEqual({ valid: [], invalid: [] });
  });

  it('parseWatchlistEntries is the raw-split layer (no validation)', () => {
    expect(parseWatchlistEntries('a, b\n c,,d')).toEqual(['a', 'b', 'c', 'd']);
    expect(parseWatchlistEntries('')).toEqual([]);
  });

  // ── Real-world URL variants — regression tests for the silent-drop bug
  // where the strict ``^...$`` validator rejected browser-copied URLs
  // (with trailing /recent-activity, ?originalSubdomain=us, mobile subdomain,
  // etc.) so they never reached the save payload.

  it('accepts URLs with trailing path segments (browser-copied)', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe/recent-activity/all/\n' +
      'https://www.linkedin.com/in/bob/details/skills/',
    );
    expect(valid).toHaveLength(2);
    expect(invalid).toHaveLength(0);
  });

  it('accepts URLs with query strings (utm/tracking params)', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe?utm_source=share\n' +
      'https://www.linkedin.com/in/bob/?originalSubdomain=us',
    );
    expect(valid).toHaveLength(2);
    expect(invalid).toHaveLength(0);
  });

  it('accepts URLs with hash fragments', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe#about',
    );
    expect(valid).toHaveLength(1);
    expect(invalid).toHaveLength(0);
  });

  it('accepts mobile (mwlite.) and bare-host LinkedIn variants', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://mwlite.linkedin.com/in/jane-doe\n' +
      'http://linkedin.com/in/bob-smith\n' +
      'https://linkedin.com/in/carol',
    );
    expect(valid).toHaveLength(3);
    expect(invalid).toHaveLength(0);
  });

  it('accepts slugs containing dots (legacy custom URLs)', () => {
    const { valid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane.doe',
    );
    expect(valid).toHaveLength(1);
  });

  it('dedupes across URL variants of the same profile', () => {
    // All four flavours of the SAME slug ``jane-doe`` — must collapse to one.
    const { valid } = classifyWatchlistEntries(
      'https://www.linkedin.com/in/jane-doe\n' +
      'https://www.linkedin.com/in/jane-doe/recent-activity/all/\n' +
      'https://mwlite.linkedin.com/in/jane-doe?utm=share\n' +
      'https://www.linkedin.com/in/Jane-Doe/details/skills/#about',
    );
    expect(valid).toHaveLength(1);
  });

  it('still rejects non-LinkedIn URLs even when permissive', () => {
    const { valid, invalid } = classifyWatchlistEntries(
      'https://www.facebook.com/jane-doe\n' +
      'https://linkedin.com/company/acme\n' +  // /company/ not /in/
      'https://www.linkedin.com/feed/\n' +     // wrong path
      'https://www.linkedin.com/in/jane-doe',  // valid
    );
    expect(valid).toHaveLength(1);
    expect(invalid).toHaveLength(3);
  });
});


describe('SocialRadar — Watchlist bulk-paste UI', () => {
  it('shows live valid/invalid counts as the user types', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));

    const watchlist = await screen.findByTestId('input-watchlist');
    // The pill starts hidden when the textarea is empty.
    expect(screen.queryByTestId('watchlist-counts')).toBeNull();

    // Paste a mix of valid + invalid via fireEvent.change (userEvent.type
    // on a long string is slow).
    fireEvent.change(watchlist, {
      target: {
        value:
          'https://www.linkedin.com/in/jane\n' +
          'https://www.linkedin.com/in/bob\n' +
          'not-a-url\n' +
          'https://twitter.com/foo',
      },
    });

    expect(await screen.findByTestId('watchlist-valid-count')).toHaveTextContent('2');
    expect(screen.getByTestId('watchlist-invalid-count')).toHaveTextContent('2');
  });

  it('save sends the deduped valid-only list, not the raw textarea', async () => {
    api.createSearch.mockResolvedValue({ ...SEARCH_FIXTURE, id: 'new' });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('new-search-btn'));

    await user.type(screen.getByTestId('input-name'), 'Watchlist test');
    await user.type(screen.getByTestId('input-topic'), 'msp pain');

    fireEvent.change(screen.getByTestId('input-watchlist'), {
      target: {
        value:
          'https://www.linkedin.com/in/jane\n' +
          'https://www.linkedin.com/in/Jane/\n' +  // dup by slug
          'garbage\n' +
          'https://www.linkedin.com/in/bob',
      },
    });

    await user.click(screen.getByTestId('save-search-btn'));
    await waitFor(() => {
      expect(api.createSearch).toHaveBeenCalledTimes(1);
      const payload = api.createSearch.mock.calls[0][0];
      // Deduped: jane + bob.  No "garbage".
      expect(payload.linkedin_profile_watchlist).toHaveLength(2);
      expect(payload.linkedin_profile_watchlist.some((u) => u.includes('jane'))).toBe(true);
      expect(payload.linkedin_profile_watchlist.some((u) => u.includes('bob'))).toBe(true);
      expect(payload.linkedin_profile_watchlist.some((u) => u.includes('garbage'))).toBe(false);
    });
  });
});
