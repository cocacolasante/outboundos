import { describe, it, expect } from 'vitest';
import client from './client.js';

describe('api client', () => {
  it('creates an axios instance with JSON headers', () => {
    expect(client.defaults.headers['Content-Type']).toBe('application/json');
    expect(typeof client.defaults.baseURL).toBe('string');
    expect(client.defaults.baseURL.length).toBeGreaterThan(0);
  });
});
