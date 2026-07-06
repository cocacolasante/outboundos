import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

// Must be before component imports so Vitest hoists correctly.
vi.mock('@xyflow/react', () => {
  const ReactFlowMock = ({ children }) => <div data-testid="react-flow-canvas">{children}</div>;
  const ReactFlowProviderMock = ({ children }) => <div>{children}</div>;
  const BackgroundMock = () => null;
  const ControlsMock = () => null;
  const HandleMock = () => null;
  return {
    ReactFlow: ReactFlowMock,
    ReactFlowProvider: ReactFlowProviderMock,
    Background: BackgroundMock,
    Controls: ControlsMock,
    Handle: HandleMock,
    MarkerType: { ArrowClosed: 'ArrowClosed' },
    Position: { Left: 'Left', Right: 'Right' },
    addEdge: vi.fn((params, edges) => [...edges, params]),
    applyNodeChanges: vi.fn((changes, nodes) => nodes),
    applyEdgeChanges: vi.fn((changes, edges) => edges),
    useReactFlow: () => ({
      screenToFlowPosition: vi.fn(({ x, y }) => ({ x, y })),
    }),
  };
});

vi.mock('@xyflow/react/dist/style.css', () => ({}));

vi.mock('../api/sequences.js', () => ({
  getSequence: vi.fn(),
  getSequenceAnalytics: vi.fn(),
  updateSequence: vi.fn(),
  validateSequence: vi.fn(),
  publishSequence: vi.fn(),
}));

import * as seqApi from '../api/sequences.js';
import SequenceBuilder, { EmbeddedSequenceBuilder } from './SequenceBuilder.jsx';

const BASE_SEQUENCE = {
  id: 'seq-1',
  campaign_id: 'c1',
  is_published: true,
  nodes: [
    {
      id: 'node-1',
      kind: 'email',
      is_entry: true,
      config: { use_campaign_compose: true },
      position_x: 0,
      position_y: 0,
    },
  ],
  edges: [],
};

function renderBuilder(campaignId = 'c1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/campaigns/${campaignId}/sequence`]}>
        <Routes>
          <Route path="/campaigns/:id/sequence" element={<SequenceBuilder />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  seqApi.getSequence.mockResolvedValue(BASE_SEQUENCE);
  seqApi.getSequenceAnalytics.mockResolvedValue({ per_node: [], lead_status: {} });
});

describe('SequenceBuilder', () => {
  it('shows loading state while fetching', () => {
    // Never resolves — keeps the component in loading state.
    seqApi.getSequence.mockReturnValue(new Promise(() => {}));

    renderBuilder();

    expect(screen.getByText(/loading sequence/i)).toBeInTheDocument();
  });

  it('renders palette with expected node kinds after load', async () => {
    renderBuilder();

    await waitFor(() => {
      expect(screen.getByTestId('react-flow-canvas')).toBeInTheDocument();
    });

    expect(screen.getByText('Email')).toBeInTheDocument();
    expect(screen.getByText('Reply')).toBeInTheDocument();
    expect(screen.getByText('Wait')).toBeInTheDocument();
    expect(screen.getByText('LI: View profile')).toBeInTheDocument();
    expect(screen.getByText('LI: Connect')).toBeInTheDocument();
    // LI: InMail used to be in the palette but it's gated out while
    // Unipile's Sales-Nav API access is locked down on our workspace —
    // publishing a sequence with it would fail.  Same for LI: Invite to
    // page (passthrough whitelist).  Test the currently-publishable set.
    expect(screen.getByText('LI: DM')).toBeInTheDocument();
    expect(screen.queryByText('LI: InMail')).not.toBeInTheDocument();
    expect(screen.queryByText('LI: Invite to page')).not.toBeInTheDocument();
  });

  it('Save draft button calls updateSequence with the campaign id', async () => {
    const user = userEvent.setup();
    seqApi.updateSequence.mockResolvedValue(BASE_SEQUENCE);

    renderBuilder();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /save draft/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => {
      expect(seqApi.updateSequence).toHaveBeenCalled();
    });
    expect(seqApi.updateSequence.mock.calls[0][0]).toBe('c1');
  });

  it('Validate button calls validateSequence and shows valid message', async () => {
    const user = userEvent.setup();
    seqApi.validateSequence.mockResolvedValue({ ok: true, errors: [] });

    renderBuilder();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /validate/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(seqApi.validateSequence).toHaveBeenCalledWith('c1');
    });
    await waitFor(() => {
      expect(screen.getByText(/sequence is valid/i)).toBeInTheDocument();
    });
  });

  it('Validate errors are displayed', async () => {
    const user = userEvent.setup();
    seqApi.validateSequence.mockResolvedValue({ ok: false, errors: ['Cycle detected'] });

    renderBuilder();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /validate/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(screen.getByText(/cycle detected/i)).toBeInTheDocument();
    });
  });

  it('Save + Publish button calls updateSequence then publishSequence', async () => {
    const user = userEvent.setup();
    seqApi.updateSequence.mockResolvedValue(BASE_SEQUENCE);
    seqApi.publishSequence.mockResolvedValue({ ok: true, errors: [] });

    renderBuilder();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /save \+ publish/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save \+ publish/i }));

    await waitFor(() => {
      expect(seqApi.updateSequence).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(seqApi.publishSequence).toHaveBeenCalled();
    });
  });

  it('shows "Published." message after Save + Publish succeeds', async () => {
    const user = userEvent.setup();
    seqApi.updateSequence.mockResolvedValue(BASE_SEQUENCE);
    seqApi.publishSequence.mockResolvedValue({ ok: true, errors: [] });

    renderBuilder();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /save \+ publish/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save \+ publish/i }));

    await waitFor(() => {
      expect(screen.getByText(/published\./i)).toBeInTheDocument();
    });
  });

  it('NodeEditor shows placeholder when no node is selected', async () => {
    renderBuilder();

    await waitFor(() => {
      expect(screen.getByTestId('react-flow-canvas')).toBeInTheDocument();
    });

    // No node is selected on load — the right panel should show the placeholder.
    expect(screen.getByText(/select a node to edit/i)).toBeInTheDocument();
  });
});

describe('EmbeddedSequenceBuilder', () => {
  function renderEmbedded(props = {}) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <EmbeddedSequenceBuilder campaignId="c1" onContinue={vi.fn()} onSkip={vi.fn()} {...props} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('renders Skip and Continue buttons', async () => {
    renderEmbedded();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /skip — use default/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /continue to upload/i })).toBeInTheDocument();
  });

  it('Skip button calls onSkip', async () => {
    const user = userEvent.setup();
    const onSkip = vi.fn();

    renderEmbedded({ onSkip });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /skip — use default/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /skip — use default/i }));
    expect(onSkip).toHaveBeenCalled();
  });
});
