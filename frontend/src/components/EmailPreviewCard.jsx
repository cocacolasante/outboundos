import { useEffect, useRef, useState } from 'react';

const QUALITY_CONFIG = {
  rich:    { borderClass: 'border-l-emerald-500', badgeClass: 'bg-emerald-100 text-emerald-700', label: 'Rich research' },
  partial: { borderClass: 'border-l-yellow-400', badgeClass: 'bg-yellow-100 text-yellow-700',   label: 'Partial research' },
  low:     { borderClass: 'border-l-slate-300',  badgeClass: 'bg-slate-100 text-slate-600',     label: 'Generic' },
};

/**
 * One reviewable sample email.
 *  - Local state for subject/body
 *  - Auto-saves on blur via onSave({composed_subject?, composed_body?})
 *  - Per-card approve/reject toggle via onApprove(true|false)
 */
export default function EmailPreviewCard({ sample, remainingSamples, onSave, onApprove }) {
  const [subject, setSubject] = useState(sample.composed_subject ?? '');
  const [body, setBody] = useState(sample.composed_body ?? '');
  const [showResearch, setShowResearch] = useState(false);
  const [saving, setSaving] = useState(false);

  // Sync local state when the parent provides an updated sample.
  const lastSampleId = useRef(sample.lead_id);
  useEffect(() => {
    if (lastSampleId.current !== sample.lead_id) {
      lastSampleId.current = sample.lead_id;
    }
    setSubject(sample.composed_subject ?? '');
    setBody(sample.composed_body ?? '');
  }, [sample.lead_id, sample.composed_subject, sample.composed_body]);

  const dirty =
    subject !== (sample.composed_subject ?? '') ||
    body !== (sample.composed_body ?? '');

  async function handleBlur() {
    if (!dirty) return;
    setSaving(true);
    try {
      const payload = {};
      if (subject !== (sample.composed_subject ?? '')) payload.composed_subject = subject;
      if (body !== (sample.composed_body ?? '')) payload.composed_body = body;
      await onSave(sample.lead_id, payload);
    } finally {
      setSaving(false);
    }
  }

  const quality = QUALITY_CONFIG[sample.research_quality] || QUALITY_CONFIG.low;

  return (
    <div
      data-testid="email-preview-card"
      data-lead-id={sample.lead_id}
      className={`bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-4 border-l-4 ${quality.borderClass}`}
    >
      <header className="flex justify-between items-start gap-3">
        <div>
          <h3 className="m-0 text-base font-semibold text-slate-900">
            {sample.first_name} {sample.last_name}
          </h3>
          <p className="mt-1 mb-0 text-slate-500 text-sm">
            {sample.job_title ? `${sample.job_title} at ` : ''}{sample.company || sample.email}
          </p>
        </div>
        <span
          data-testid="quality-badge"
          data-quality={sample.research_quality}
          className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${quality.badgeClass}`}
        >
          {quality.label}
        </span>
      </header>

      <button
        type="button"
        onClick={() => setShowResearch((v) => !v)}
        className="mt-3 text-sm text-brand-600 hover:text-brand-800 hover:underline bg-transparent border-none cursor-pointer p-0"
        aria-expanded={showResearch}
      >
        {showResearch ? '▾ Hide research' : '▸ Show research'}
      </button>
      {showResearch && (
        <div data-testid="research-panel" className="bg-slate-50 rounded-lg p-3 mt-2 text-sm text-slate-700">
          {sample.research_summary || 'No research findings.'}
        </div>
      )}

      <div className="mt-3">
        <label htmlFor={`subject-${sample.lead_id}`} className="block text-sm font-medium text-slate-700 mb-1">Subject</label>
        <input
          id={`subject-${sample.lead_id}`}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          onBlur={handleBlur}
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
        />
      </div>

      <div className="mt-3">
        <label htmlFor={`body-${sample.lead_id}`} className="block text-sm font-medium text-slate-700 mb-1">Body</label>
        <textarea
          id={`body-${sample.lead_id}`}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onBlur={handleBlur}
          rows={8}
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white font-[inherit] resize-y"
        />
      </div>

      {dirty && remainingSamples > 0 && (
        <p data-testid="dirty-hint" className="text-xs text-amber-700 mt-1 mb-0">
          Your edits will improve the remaining {remainingSamples} {remainingSamples === 1 ? 'email' : 'emails'}.
        </p>
      )}

      {saving && <p className="text-xs text-slate-400 mt-1">Saving…</p>}

      <div className="flex gap-2 mt-4">
        <button
          type="button"
          onClick={() => onApprove(sample.lead_id, true)}
          className={`inline-flex items-center px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
            sample.sample_approved === true
              ? 'bg-emerald-600 hover:bg-emerald-700 text-white'
              : 'bg-white hover:bg-slate-50 text-slate-700 border border-slate-300'
          }`}
          data-testid="approve-button"
          aria-pressed={sample.sample_approved === true}
        >
          ✓ Approve
        </button>
        <button
          type="button"
          onClick={() => onApprove(sample.lead_id, false)}
          className={`inline-flex items-center px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
            sample.sample_approved === false
              ? 'bg-red-600 hover:bg-red-700 text-white'
              : 'bg-white hover:bg-slate-50 text-slate-700 border border-slate-300'
          }`}
          data-testid="reject-button"
          aria-pressed={sample.sample_approved === false}
        >
          ✗ Reject
        </button>
      </div>
    </div>
  );
}
