import { useState } from 'react';
import { uploadLeadsPreview, confirmLeadsUpload } from '../api/campaigns.js';

const LEAD_FIELDS = [
  { value: '', label: '— ignore —' },
  { value: 'email', label: 'email *' },
  { value: 'first_name', label: 'first_name' },
  { value: 'last_name', label: 'last_name' },
  { value: 'company', label: 'company' },
  { value: 'company_website', label: 'company_website' },
  { value: 'job_title', label: 'job_title' },
  { value: 'linkedin_url', label: 'linkedin_url' },
  { value: 'phone', label: 'phone' },
];

// Template columns in the order users typically expect them. Keep `email`
// first since it's the only required field. Matches _ALLOWED_LEAD_FIELDS in
// backend/app/routers/leads.py.
const TEMPLATE_COLUMNS = [
  'email',
  'first_name',
  'last_name',
  'company',
  'company_website',
  'job_title',
  'linkedin_url',
  'phone',
];

const TEMPLATE_SAMPLE_ROW = [
  'jane.doe@example.com',
  'Jane',
  'Doe',
  'Acme Inc',
  'https://www.acme.com',
  'VP Marketing',
  'https://www.linkedin.com/in/janedoe/',
  '+1-555-0100',
];


function escapeCsvCell(value) {
  // Quote when the cell contains a comma, quote, or newline. Double any
  // embedded quotes per RFC 4180.
  const s = String(value ?? '');
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}


function downloadLeadsTemplate() {
  const lines = [
    TEMPLATE_COLUMNS.join(','),
    TEMPLATE_SAMPLE_ROW.map(escapeCsvCell).join(','),
  ];
  // BOM so Excel detects UTF-8 cleanly.
  const blob = new Blob(['﻿', lines.join('\n')], {
    type: 'text/csv;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'leads-template.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Give the browser a tick before revoking, otherwise some browsers cancel
  // the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Two-stage uploader.  After file selection, hits /upload to preview rows +
 * suggested mapping, lets the user adjust per-column mapping, then calls
 * /confirm-upload.
 */
export default function LeadUpload({ campaignId, onComplete }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [mapping, setMapping] = useState({});
  const [loading, setLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState(null);

  async function handleFileSelect(e) {
    const f = e.target.files?.[0];
    if (!f) return;
    setFile(f);
    setError(null);
    setLoading(true);
    try {
      const data = await uploadLeadsPreview(campaignId, f);
      setPreview(data);
      setMapping(data.suggested_mapping || {});
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Upload failed');
      setFile(null);
    } finally {
      setLoading(false);
    }
  }

  function updateMapping(col, field) {
    setMapping((m) => {
      const next = { ...m };
      if (field) next[col] = field;
      else delete next[col];
      return next;
    });
  }

  async function handleConfirm() {
    setError(null);
    const hasEmail = Object.values(mapping).includes('email');
    if (!hasEmail) {
      setError('You must map a CSV column to "email".');
      return;
    }
    setConfirming(true);
    try {
      const result = await confirmLeadsUpload(campaignId, file, mapping);
      onComplete(result);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Confirm failed');
    } finally {
      setConfirming(false);
    }
  }

  return (
    <div data-testid="lead-upload">
      <h2 className="mt-0 text-xl font-bold text-slate-900 mb-4">Upload leads</h2>

      {!preview && (
        <div>
          <p className="text-slate-600 text-sm mb-4">
            Upload a CSV of your leads. We'll suggest column mappings on the next step.
          </p>

          <div className="mb-6 p-4 bg-slate-50 border border-slate-200 rounded-lg">
            <div className="flex items-start justify-between gap-3">
              <div className="text-sm text-slate-700">
                <div className="font-medium text-slate-900 mb-1">Need a template?</div>
                <div className="text-xs text-slate-600">
                  Supported columns:{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">email *</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">first_name</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">last_name</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">company</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">company_website</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">job_title</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">linkedin_url</code>{' '}
                  <code className="bg-white px-1 py-0.5 rounded border border-slate-200">phone</code>
                  <span className="ml-1">— only <strong>email</strong> is required; <strong>company_website</strong> improves AI research quality.</span>
                </div>
              </div>
              <button
                type="button"
                onClick={downloadLeadsTemplate}
                data-testid="download-template"
                className="inline-flex items-center gap-1.5 shrink-0 px-3 py-1.5 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium border border-slate-300 rounded-lg transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
                </svg>
                Download template
              </button>
            </div>
          </div>

          <label className="inline-flex flex-col items-center justify-center w-full border-2 border-dashed border-slate-300 rounded-xl p-10 text-center hover:border-brand-400 cursor-pointer transition-colors">
            <svg className="w-10 h-10 text-slate-400 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
            </svg>
            <span className="text-sm font-medium text-brand-600">
              {loading ? 'Parsing…' : 'Choose CSV'}
            </span>
            <span className="text-xs text-slate-500 mt-1">or drag and drop a file</span>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={handleFileSelect}
              disabled={loading}
              className="hidden"
              data-testid="file-input"
            />
          </label>
        </div>
      )}

      {preview && (
        <div>
          <p className="text-sm text-slate-700 mb-4">
            <strong className="font-semibold">{preview.total_rows} rows</strong> detected in <code className="bg-slate-100 px-1 py-0.5 rounded text-xs">{file?.name}</code>.
            Map each CSV column to a lead field below — <em>email is required</em>.
          </p>

          <div className="overflow-hidden rounded-xl border border-slate-200 mb-4">
            <table data-testid="mapping-table" className="w-full text-sm">
              <thead>
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">CSV column</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Map to</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider bg-slate-50 border-b border-slate-200">Sample value</th>
                </tr>
              </thead>
              <tbody>
                {preview.columns.map((col) => (
                  <tr key={col} className="hover:bg-slate-50">
                    <td className="px-4 py-3 text-slate-700 border-b border-slate-100">
                      <code className="bg-slate-100 px-1 py-0.5 rounded text-xs">{col}</code>
                    </td>
                    <td className="px-4 py-3 text-slate-700 border-b border-slate-100">
                      <select
                        aria-label={`Map ${col}`}
                        value={mapping[col] || ''}
                        onChange={(e) => updateMapping(col, e.target.value)}
                        className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
                      >
                        {LEAD_FIELDS.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-3 text-slate-500 border-b border-slate-100 text-xs">
                      {preview.preview_rows[0]?.[col] || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {error && (
            <div data-testid="upload-error" className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800 mb-4">{error}</div>
          )}

          <div className="flex gap-3 justify-end">
            <button
              type="button"
              onClick={() => { setPreview(null); setFile(null); setError(null); }}
              className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
            >
              Choose different file
            </button>
            <button
              type="button"
              onClick={handleConfirm}
              disabled={confirming}
              className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {confirming ? 'Importing…' : `Import ${preview.total_rows} leads`}
            </button>
          </div>
        </div>
      )}

      {!preview && error && (
        <div data-testid="upload-error" className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800 mt-4">{error}</div>
      )}
    </div>
  );
}
