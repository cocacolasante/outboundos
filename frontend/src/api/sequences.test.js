import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client.js', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

import client from './client.js';
import {
  getSequence,
  updateSequence,
  validateSequence,
  publishSequence,
  getSequenceAnalytics,
} from './sequences.js';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getSequence', () => {
  it('calls GET /campaigns/{id}/sequence and returns data', async () => {
    const sequence = { id: 'seq-1', campaign_id: 'c1', nodes: [], edges: [] };
    client.get.mockResolvedValue({ data: sequence });

    const result = await getSequence('c1');

    expect(client.get).toHaveBeenCalledWith('/campaigns/c1/sequence');
    expect(result).toEqual(sequence);
  });
});

describe('updateSequence', () => {
  it('calls PUT /campaigns/{id}/sequence with payload and returns data', async () => {
    const payload = { nodes: [], edges: [] };
    const updated = { id: 'seq-1', campaign_id: 'c1', ...payload };
    client.put.mockResolvedValue({ data: updated });

    const result = await updateSequence('c1', payload);

    expect(client.put).toHaveBeenCalledWith('/campaigns/c1/sequence', payload);
    expect(result).toEqual(updated);
  });
});

describe('validateSequence', () => {
  it('calls POST /campaigns/{id}/sequence/validate and returns data', async () => {
    const validation = { ok: true, errors: [] };
    client.post.mockResolvedValue({ data: validation });

    const result = await validateSequence('c1');

    expect(client.post).toHaveBeenCalledWith('/campaigns/c1/sequence/validate');
    expect(result).toEqual(validation);
  });
});

describe('publishSequence', () => {
  it('calls POST /campaigns/{id}/sequence/publish and returns data', async () => {
    const published = { ok: true };
    client.post.mockResolvedValue({ data: published });

    const result = await publishSequence('c1');

    expect(client.post).toHaveBeenCalledWith('/campaigns/c1/sequence/publish');
    expect(result).toEqual(published);
  });
});

describe('getSequenceAnalytics', () => {
  it('calls GET /campaigns/{id}/sequence/analytics and returns data', async () => {
    const analytics = { per_node: [], lead_status: {} };
    client.get.mockResolvedValue({ data: analytics });

    const result = await getSequenceAnalytics('c1');

    expect(client.get).toHaveBeenCalledWith('/campaigns/c1/sequence/analytics');
    expect(result).toEqual(analytics);
  });
});
