/**
 * Single source of truth for status / badge colours (Phase 1, UI refinement).
 *
 * Today each page redefines its own status→class map (Leads STATUS_CLASSES +
 * CRM_STATUS_CLASSES, Analytics SEQ_KIND_COLORS, CampaignDetail PILL,
 * Replies/Signals badges) with drifting semantics.  This module collapses them
 * onto ONE semantic palette + per-domain mappings, so a "sent"/"won"/"positive"
 * chip looks the same everywhere.
 *
 * Usage:
 *   import { chipClasses, sendStatusSemantic } from '../utils/statusColors.js';
 *   <span className={chipClasses(sendStatusSemantic(lead.send_status))}>…</span>
 */

// The semantic palette — the only colours a status chip may use.
export const SEMANTIC = {
  neutral: 'bg-slate-100 text-slate-600',
  brand: 'bg-brand-100 text-brand-700',
  info: 'bg-info-100 text-info-700',
  success: 'bg-success-100 text-success-700',
  warning: 'bg-warning-100 text-warning-700',
  danger: 'bg-danger-100 text-danger-700',
  purple: 'bg-purple-100 text-purple-700',
};

const PILL_BASE =
  'inline-flex items-center px-1.5 py-0.5 rounded-pill text-[10px] font-medium';

/** Tailwind classes for a status pill of the given semantic key. */
export function chipClasses(semantic = 'neutral', { withBase = true } = {}) {
  const tone = SEMANTIC[semantic] || SEMANTIC.neutral;
  return withBase ? `${PILL_BASE} ${tone}` : tone;
}

// ---- Per-domain value → semantic mappings --------------------------------

const SEND_STATUS = {
  pending: 'neutral', running: 'info', scheduled: 'warning',
  sent: 'success', delivered: 'success', failed: 'danger',
  suppressed: 'danger', skipped: 'neutral', done: 'success',
};
const CRM_STATUS = {
  new: 'info', working: 'warning', qualified: 'success',
  converted: 'purple', unqualified: 'neutral',
};
const OPP_STAGE = {
  prospecting: 'info', qualification: 'warning', proposal: 'brand',
  negotiation: 'purple', closed_won: 'success', closed_lost: 'neutral',
};
const SENTIMENT = {
  positive: 'success', neutral: 'neutral', negative: 'danger',
};
const SEQUENCE_STATUS = {
  active: 'info', completed: 'success', halted: 'danger', pending: 'neutral',
};

const _lookup = (map, value, fallback = 'neutral') =>
  map[String(value || '').toLowerCase()] || fallback;

export const sendStatusSemantic = (v) => _lookup(SEND_STATUS, v);
export const crmStatusSemantic = (v) => _lookup(CRM_STATUS, v);
export const oppStageSemantic = (v) => _lookup(OPP_STAGE, v);
export const sentimentSemantic = (v) => _lookup(SENTIMENT, v);
export const sequenceStatusSemantic = (v) => _lookup(SEQUENCE_STATUS, v);
