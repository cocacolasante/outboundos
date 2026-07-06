import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client.js', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

import client from './client.js';
import {
  listLinkedInAccounts,
  getLinkedInAccount,
  updateLinkedInAccount,
  deleteLinkedInAccount,
  testLinkedInAccount,
  resolveLinkedInChallenge,
} from './linkedinAccounts.js';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('listLinkedInAccounts', () => {
  it('calls GET /linkedin-accounts/ and returns data', async () => {
    const accounts = [{ id: '1', label: 'Test' }];
    client.get.mockResolvedValue({ data: accounts });

    const result = await listLinkedInAccounts();

    expect(client.get).toHaveBeenCalledWith('/linkedin-accounts/');
    expect(result).toEqual(accounts);
  });
});

describe('getLinkedInAccount', () => {
  it('calls GET /linkedin-accounts/{id} and returns data', async () => {
    const account = { id: 'acc-42', label: 'My Account' };
    client.get.mockResolvedValue({ data: account });

    const result = await getLinkedInAccount('acc-42');

    expect(client.get).toHaveBeenCalledWith('/linkedin-accounts/acc-42');
    expect(result).toEqual(account);
  });
});

describe('updateLinkedInAccount', () => {
  it('calls PATCH /linkedin-accounts/{id} with payload and returns data', async () => {
    const payload = { label: 'Updated label' };
    const updated = { id: 'acc-1', label: 'Updated label' };
    client.patch.mockResolvedValue({ data: updated });

    const result = await updateLinkedInAccount('acc-1', payload);

    expect(client.patch).toHaveBeenCalledWith('/linkedin-accounts/acc-1', payload);
    expect(result).toEqual(updated);
  });
});

describe('deleteLinkedInAccount', () => {
  it('calls DELETE /linkedin-accounts/{id} and returns void', async () => {
    client.delete.mockResolvedValue({});

    const result = await deleteLinkedInAccount('acc-1');

    expect(client.delete).toHaveBeenCalledWith('/linkedin-accounts/acc-1');
    expect(result).toBeUndefined();
  });
});

describe('testLinkedInAccount', () => {
  it('calls POST /linkedin-accounts/{id}/test and returns data', async () => {
    const testResponse = { ok: true, status: 'ok' };
    client.post.mockResolvedValue({ data: testResponse });

    const result = await testLinkedInAccount('acc-1');

    expect(client.post).toHaveBeenCalledWith('/linkedin-accounts/acc-1/test');
    expect(result).toEqual(testResponse);
  });
});

describe('resolveLinkedInChallenge', () => {
  it('calls POST /linkedin-accounts/{id}/resolve-challenge and returns data', async () => {
    const resolveResponse = { ok: true };
    client.post.mockResolvedValue({ data: resolveResponse });

    const result = await resolveLinkedInChallenge('acc-1');

    expect(client.post).toHaveBeenCalledWith('/linkedin-accounts/acc-1/resolve-challenge', {});
    expect(result).toEqual(resolveResponse);
  });
});
