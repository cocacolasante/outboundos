import { describe, it, expect } from 'vitest';
import {
  EMPTY, formatNumber, formatCurrency, formatPercent, formatDate, formatDateTime,
} from './format.js';

describe('shared formatters', () => {
  it('formatNumber groups and handles null', () => {
    expect(formatNumber(1234)).toBe('1,234');
    expect(formatNumber(null)).toBe(EMPTY);
    expect(formatNumber(undefined)).toBe(EMPTY);
  });

  it('formatCurrency renders USD, no cents, null -> em dash', () => {
    expect(formatCurrency(10000)).toMatch(/\$10,000/);
    expect(formatCurrency(null)).toBe('—');
  });

  it('formatPercent renders a 0-1 fraction; null -> em dash (standardized)', () => {
    expect(formatPercent(0.512)).toBe('51.2%');
    expect(formatPercent(0)).toBe('0.0%');
    expect(formatPercent(null)).toBe('—');
  });

  it('formatDate / formatDateTime handle empties', () => {
    expect(formatDate(null)).toBe('—');
    expect(formatDate('not-a-date')).toBe('—');
    expect(formatDateTime(null)).toBe('—');
    expect(formatDate('2026-07-15T00:00:00Z')).not.toBe('—');
  });
});
