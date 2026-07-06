import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ConnectLinkedInModal from './ConnectLinkedInModal.jsx';

vi.mock('../api/linkedinAccounts.js', () => ({
  connectViaUnipile: vi.fn(),
  deleteLinkedInAccount: vi.fn(),
  getLinkedInAccount: vi.fn(),
  importFromUnipile: vi.fn(),
  listDiscoverableUnipileAccounts: vi.fn(),
  resolveLinkedInChallenge: vi.fn(),
  syncUnipileStatus: vi.fn(),
}));

import * as liApi from '../api/linkedinAccounts.js';

const ACCOUNT = {
  id: 'acc-1',
  label: 'My LinkedIn',
  linkedin_email: 'me@example.com',
  status: 'ok',
  pending_challenge_url: null,
};

function renderNew(props = {}) {
  return render(<ConnectLinkedInModal onClose={vi.fn()} onSaved={vi.fn()} {...props} />);
}

function renderEdit(overrides = {}) {
  return render(
    <ConnectLinkedInModal
      account={{ ...ACCOUNT, ...overrides }}
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  liApi.listDiscoverableUnipileAccounts.mockResolvedValue([]);
  // window.open is invoked when launching the hosted flow; stub it so jsdom
  // doesn't actually try to open a window.
  vi.stubGlobal('open', vi.fn());
});

describe('ConnectLinkedInModal', () => {
  it('renders "Connect LinkedIn account" when no account prop', async () => {
    renderNew();
    expect(screen.getByRole('heading', { name: /connect linkedin account/i })).toBeInTheDocument();
    // The Unipile-only flow shows the label input + the launch button.
    expect(screen.getByLabelText(/label/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /connect via unipile/i })).toBeInTheDocument();
  });

  it('renders "Edit LinkedIn account" when an account prop is passed', () => {
    renderEdit();
    expect(screen.getByRole('heading', { name: /edit linkedin account/i })).toBeInTheDocument();
    expect(screen.getByText(ACCOUNT.label)).toBeInTheDocument();
    expect(screen.getByText(ACCOUNT.linkedin_email)).toBeInTheDocument();
  });

  it('Connect via Unipile button requires a label', async () => {
    const user = userEvent.setup();
    renderNew();
    await user.click(screen.getByRole('button', { name: /connect via unipile/i }));
    await waitFor(() => {
      expect(screen.getByTestId('modal-error')).toHaveTextContent(/label/i);
    });
    expect(liApi.connectViaUnipile).not.toHaveBeenCalled();
  });

  it('Connect via Unipile calls the API and opens hosted URL', async () => {
    const user = userEvent.setup();
    liApi.connectViaUnipile.mockResolvedValue({
      account_id: 'new-acc-1',
      hosted_url: 'https://hosted.unipile.com/x',
    });

    renderNew();
    await user.type(screen.getByLabelText(/label/i), 'Anthony — main');
    await user.click(screen.getByRole('button', { name: /connect via unipile/i }));

    await waitFor(() => {
      expect(liApi.connectViaUnipile).toHaveBeenCalled();
    });
    const payload = liApi.connectViaUnipile.mock.calls[0][0];
    expect(payload.label).toBe('Anthony — main');
    expect(payload.success_redirect_url).toContain('unipile=success');
    expect(window.open).toHaveBeenCalledWith(
      'https://hosted.unipile.com/x',
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('surfaces unipile API errors', async () => {
    const user = userEvent.setup();
    liApi.connectViaUnipile.mockRejectedValue({
      response: { data: { detail: 'Unipile not configured' } },
    });

    renderNew();
    await user.type(screen.getByLabelText(/label/i), 'x');
    await user.click(screen.getByRole('button', { name: /connect via unipile/i }));

    await waitFor(() => {
      expect(screen.getByTestId('modal-error')).toHaveTextContent('Unipile not configured');
    });
  });

  it('shows discoverable Unipile accounts when listDiscoverable returns rows', async () => {
    liApi.listDiscoverableUnipileAccounts.mockResolvedValue([
      {
        unipile_account_id: 'u-1',
        name: 'Alice Smith',
        public_identifier: 'alice-smith',
        status: 'CONNECTED',
      },
    ]);
    renderNew();
    await waitFor(() => {
      expect(screen.getByTestId('unipile-import-section')).toBeInTheDocument();
    });
    expect(screen.getByText(/Already connected in Unipile/i)).toBeInTheDocument();
  });

  it('clicking Import triggers importFromUnipile and closes', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSaved = vi.fn();
    liApi.listDiscoverableUnipileAccounts.mockResolvedValue([
      { unipile_account_id: 'u-1', name: 'Alice', public_identifier: 'alice' },
    ]);
    liApi.importFromUnipile.mockResolvedValue({ id: 'local-1', linkedin_email: 'alice@x.com' });

    renderNew({ onClose, onSaved });
    await waitFor(() => screen.getByTestId('unipile-import-section'));
    // Expand the panel.
    await user.click(screen.getByRole('button', { name: /already connected in unipile/i }));
    await user.click(screen.getByRole('button', { name: /^import$/i }));

    await waitFor(() => {
      expect(liApi.importFromUnipile).toHaveBeenCalledWith({
        unipile_account_id: 'u-1',
        label: 'Alice',
      });
    });
    expect(onSaved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('edit view shows clear-challenge action when account is challenged', async () => {
    const user = userEvent.setup();
    liApi.resolveLinkedInChallenge.mockResolvedValue({});
    renderEdit({ status: 'challenged' });

    const clearBtn = await screen.findByRole('button', { name: /clear challenge state/i });
    await user.click(clearBtn);
    await waitFor(() => {
      expect(liApi.resolveLinkedInChallenge).toHaveBeenCalledWith(ACCOUNT.id);
    });
  });

  it('close button (×) calls onClose', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderNew({ onClose });
    await user.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it('overlay click calls onClose', () => {
    const onClose = vi.fn();
    renderNew({ onClose });
    fireEvent.click(screen.getByTestId('modal-overlay'));
    expect(onClose).toHaveBeenCalled();
  });
});
