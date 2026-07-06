/**
 * Shared number / currency / percent / date formatting — the single source of
 * truth so every analytics surface renders the same value the same way.
 *
 * Standardized placeholder for null/undefined is the EM DASH "—" (previously
 * Analytics used "--" and others used "—").  See docs/crm-metrics.md.
 */

export const EMPTY = '—';

/** Whole number with grouping (1234 -> "1,234"). */
export function formatNumber(n, { maximumFractionDigits = 0 } = {}) {
  if (n == null || Number.isNaN(n)) return EMPTY;
  return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(n);
}

/** Currency, no cents by default (10000 -> "$10,000"). */
export function formatCurrency(n, { currency = 'USD', maximumFractionDigits = 0 } = {}) {
  if (n == null || Number.isNaN(n)) return EMPTY;
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency, maximumFractionDigits,
  }).format(n);
}

/** A 0–1 fraction as a percent (0.512 -> "51.2%").  Null -> em dash. */
export function formatPercent(v, { digits = 1 } = {}) {
  if (v == null || Number.isNaN(v)) return EMPTY;
  return `${(v * 100).toFixed(digits)}%`;
}

/** Date only (locale short). */
export function formatDate(iso) {
  if (!iso) return EMPTY;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? EMPTY : d.toLocaleDateString();
}

/** Date + time (locale short). */
export function formatDateTime(iso) {
  if (!iso) return EMPTY;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? EMPTY : d.toLocaleString();
}
