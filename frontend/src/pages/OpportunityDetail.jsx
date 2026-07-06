import { useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  addProduct,
  deleteDocument,
  deleteOpportunity,
  deleteProduct,
  documentDownloadUrl,
  getOpportunity,
  listDocuments,
  listProducts,
  updateOpportunity,
  updateProduct,
  uploadDocument,
} from '../api/crm.js';
import { useToast } from '../components/Toast.jsx';
import ActivityLog from '../components/ActivityLog.jsx';
import { Skeleton } from '../components/states.jsx';
import { STAGES, fmtAmount } from './Opportunities.jsx';

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString();
}

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}


export default function OpportunityDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: opp, isLoading } = useQuery({
    queryKey: ['crm-opportunity', id],
    queryFn: () => getOpportunity(id),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['crm-opportunity', id] });
    queryClient.invalidateQueries({ queryKey: ['crm-opportunities'] });
    queryClient.invalidateQueries({ queryKey: ['crm-pipeline'] });
  };

  const updateMut = useMutation({
    mutationFn: (payload) => updateOpportunity(id, payload),
    onSuccess: () => { invalidate(); },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to save'),
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteOpportunity(id),
    onSuccess: () => {
      invalidate();
      queryClient.invalidateQueries({ queryKey: ['all-leads'] });
      toast.success('Opportunity deleted');
      navigate('/opportunities');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  // Closed-lost flow: stage button opens an inline reason prompt instead
  // of flipping immediately (Salesforce asks for a reason at close-lost).
  const [losing, setLosing] = useState(false);
  const [lossReason, setLossReason] = useState('');

  if (isLoading) {
    return (
      <div className="p-6 max-w-[70rem] mx-auto space-y-4" data-testid="opp-detail-loading">
        <Skeleton className="h-7 w-1/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (!opp) {
    return (
      <div className="p-6">
        <p className="text-slate-500">Opportunity not found.</p>
        <Link to="/opportunities" className="text-brand-600 hover:underline text-sm">
          ← Back to pipeline
        </Link>
      </div>
    );
  }

  const isClosed = opp.stage.startsWith('closed_');
  const contactName = [opp.first_name, opp.last_name].filter(Boolean).join(' ');

  function pickStage(stageValue) {
    if (stageValue === opp.stage) return;
    if (stageValue === 'closed_lost') {
      setLosing(true);  // collect the reason first
      return;
    }
    setLosing(false);
    if (stageValue === 'closed_won') {
      if (!confirm(`Mark "${opp.name}" as CLOSED WON? 🎉`)) return;
    }
    updateMut.mutate({ stage: stageValue });
  }

  return (
    <div className="p-6 max-w-5xl mx-auto" data-testid="opportunity-detail-page">
      <Link to="/opportunities" className="text-sm text-brand-600 hover:underline">
        ← Pipeline
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between mt-2 mb-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 m-0" data-testid="opp-name">
            {opp.name}
          </h1>
          <div className="text-sm text-slate-500 mt-1">
            {contactName}
            {opp.job_title && <> · {opp.job_title}</>}
            {opp.company && <> at {opp.company}</>}
          </div>
          <div className="text-xs text-slate-400 mt-0.5">
            {opp.email && (
              <a href={`mailto:${opp.email}`} className="text-brand-600 hover:underline">{opp.email}</a>
            )}
            {opp.phone && <> · {opp.phone}</>}
            {opp.linkedin_url && (
              <>
                {' · '}
                <a href={opp.linkedin_url} target="_blank" rel="noopener noreferrer"
                  className="text-brand-600 hover:underline">LinkedIn ↗</a>
              </>
            )}
          </div>
        </div>
        <div className="text-right">
          <div className="text-2xl font-bold text-slate-900">{fmtAmount(opp.amount)}</div>
          <div className="text-xs text-slate-400">
            {opp.probability != null && <>{opp.probability}% probability</>}
            {opp.close_date && <> · closes {fmtDate(opp.close_date)}</>}
          </div>
          {isClosed && (
            <div
              data-testid="closed-banner"
              className={`mt-1 inline-block px-2 py-0.5 rounded text-xs font-semibold ${
                opp.stage === 'closed_won'
                  ? 'bg-emerald-100 text-emerald-800'
                  : 'bg-slate-200 text-slate-600'
              }`}
            >
              {opp.stage === 'closed_won' ? '🏆 Won' : 'Lost'}
              {opp.closed_at && <> · {fmtDate(opp.closed_at)}</>}
            </div>
          )}
        </div>
      </div>

      {/* Stage stepper */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4 mb-4">
        <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Stage</div>
        <div className="flex flex-wrap gap-1.5" data-testid="stage-stepper">
          {STAGES.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => pickStage(s.value)}
              disabled={updateMut.isPending}
              aria-pressed={opp.stage === s.value}
              data-testid={`set-stage-${s.value}`}
              className={`px-3 py-1.5 text-xs font-medium rounded-md border ${
                opp.stage === s.value
                  ? (s.value === 'closed_won'
                    ? 'bg-emerald-600 text-white border-emerald-600'
                    : s.value === 'closed_lost'
                    ? 'bg-slate-500 text-white border-slate-500'
                    : 'bg-brand-600 text-white border-brand-600')
                  : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-100'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        {losing && (
          <div className="mt-3 flex items-end gap-2" data-testid="loss-reason-form">
            <div className="flex-1">
              <label className="block text-xs font-semibold text-slate-600 mb-1">
                Loss reason (required)
              </label>
              <input
                type="text"
                value={lossReason}
                onChange={(e) => setLossReason(e.target.value)}
                placeholder="e.g. went with incumbent, budget cut, timing…"
                data-testid="loss-reason-input"
                autoFocus
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
              />
            </div>
            <button
              type="button"
              onClick={() => {
                updateMut.mutate({ stage: 'closed_lost', loss_reason: lossReason.trim() });
                setLosing(false);
              }}
              disabled={!lossReason.trim() || updateMut.isPending}
              data-testid="confirm-closed-lost"
              className="px-3 py-2 text-sm font-medium bg-slate-600 hover:bg-slate-700 disabled:opacity-50 text-white rounded-lg"
            >
              Mark lost
            </button>
            <button
              type="button"
              onClick={() => setLosing(false)}
              className="px-3 py-2 text-sm border border-slate-300 text-slate-700 rounded-lg hover:bg-slate-50"
            >
              Cancel
            </button>
          </div>
        )}
        {opp.stage === 'closed_lost' && opp.loss_reason && !losing && (
          <p className="text-sm text-slate-500 mt-2 m-0" data-testid="loss-reason-display">
            Loss reason: <span className="text-slate-700">{opp.loss_reason}</span>
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left column: details + products + documents */}
        <div className="space-y-4">
          <DetailsCard opp={opp} onSave={(payload) => updateMut.mutate(payload)} />
          <ProductsCard oppId={id} dealAmount={opp.amount} />
          <DocumentsCard oppId={id} />
        </div>

        {/* Right column: activities + meta */}
        <div className="space-y-4">
          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4">
            <ActivityLog opportunityId={id} />
          </div>
          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4 text-xs text-slate-500">
            <div>Created {fmtDate(opp.created_at)} · last touched {fmtDate(opp.updated_at)}</div>
            {opp.source_lead_id && (
              <div className="mt-1">
                Converted from a lead —{' '}
                <Link to="/leads" className="text-brand-600 hover:underline">
                  find them in Leads
                </Link>
              </div>
            )}
            <button
              type="button"
              onClick={() => {
                if (confirm(`Delete "${opp.name}"? Documents, products, and logged activities on this deal are deleted too. The source lead (if any) becomes convertible again.`)) {
                  deleteMut.mutate();
                }
              }}
              data-testid="delete-opportunity-btn"
              className="mt-3 px-3 py-1.5 text-xs font-medium border border-red-200 text-red-600 hover:bg-red-50 rounded-lg"
            >
              Delete opportunity
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


function DetailsCard({ opp, onSave }) {
  const [amount, setAmount] = useState(opp.amount ?? '');
  const [closeDate, setCloseDate] = useState(opp.close_date ?? '');
  const [probability, setProbability] = useState(opp.probability ?? '');
  const [description, setDescription] = useState(opp.description ?? '');

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4" data-testid="details-card">
      <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">Details</div>
      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Amount ($)</label>
          <input
            type="number" min="0" step="100"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            onBlur={() => onSave({ amount: amount === '' ? null : Number(amount) })}
            data-testid="opp-amount"
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Expected close</label>
          <input
            type="date"
            value={closeDate || ''}
            onChange={(e) => setCloseDate(e.target.value)}
            onBlur={() => onSave({ close_date: closeDate || null })}
            data-testid="opp-close-date"
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
          />
        </div>
      </div>
      <div className="mb-3">
        <label className="block text-xs font-semibold text-slate-600 mb-1">Win probability (%)</label>
        <input
          type="number" min="0" max="100"
          value={probability}
          onChange={(e) => setProbability(e.target.value)}
          onBlur={() => onSave({ probability: probability === '' ? null : Number(probability) })}
          data-testid="opp-probability"
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
        />
        <p className="text-xs text-slate-400 mt-1 m-0">
          Auto-set from the stage; override here if you know better.
        </p>
      </div>
      <div>
        <label className="block text-xs font-semibold text-slate-600 mb-1">Description</label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          onBlur={() => onSave({ description: description.trim() || null })}
          rows={3}
          placeholder="Deal context, decision process, competitors…"
          data-testid="opp-description"
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
        />
      </div>
    </div>
  );
}


function ProductsCard({ oppId, dealAmount }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const queryKey = ['crm-products', oppId];

  const { data } = useQuery({ queryKey, queryFn: () => listProducts(oppId) });
  const products = data?.items ?? [];
  const productsTotal = data?.products_total ?? 0;

  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [qty, setQty] = useState('1');
  const [price, setPrice] = useState('');

  const invalidate = () => queryClient.invalidateQueries({ queryKey });

  const addMut = useMutation({
    mutationFn: () => addProduct(oppId, {
      product_name: name.trim(),
      quantity: Number(qty) || 1,
      unit_price: price === '' ? null : Number(price),
    }),
    onSuccess: () => {
      invalidate();
      setName(''); setQty('1'); setPrice(''); setAdding(false);
      toast.success('Product added');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to add product'),
  });

  const deleteMut = useMutation({
    mutationFn: (pid) => deleteProduct(pid),
    onSuccess: invalidate,
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  const qtyMut = useMutation({
    mutationFn: ({ pid, quantity }) => updateProduct(pid, { quantity }),
    onSuccess: invalidate,
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to update'),
  });

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4" data-testid="products-card">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
          Products of interest
        </span>
        <button
          type="button"
          onClick={() => setAdding((v) => !v)}
          data-testid="add-product-toggle"
          className="px-2.5 py-1 text-xs font-medium bg-brand-600 hover:bg-brand-700 text-white rounded-md"
        >
          {adding ? 'Cancel' : '+ Add product'}
        </button>
      </div>

      {adding && (
        <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 mb-3 space-y-2" data-testid="add-product-form">
          <input
            type="text" value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Product / service — e.g. Managed IT (24 seats)"
            data-testid="product-name-input"
            className="w-full px-3 py-1.5 border border-slate-300 rounded-md text-sm bg-white"
          />
          <div className="flex gap-2">
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              Qty
              <input
                type="number" min="0.01" step="1" value={qty}
                onChange={(e) => setQty(e.target.value)}
                data-testid="product-qty-input"
                className="w-20 px-2 py-1.5 border border-slate-300 rounded-md text-sm bg-white"
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              Unit price ($)
              <input
                type="number" min="0" step="50" value={price}
                onChange={(e) => setPrice(e.target.value)}
                data-testid="product-price-input"
                className="w-28 px-2 py-1.5 border border-slate-300 rounded-md text-sm bg-white"
              />
            </label>
            <button
              type="button"
              onClick={() => addMut.mutate()}
              disabled={!name.trim() || addMut.isPending}
              data-testid="product-save-btn"
              className="ml-auto px-3 py-1.5 text-xs font-medium bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white rounded-md"
            >
              {addMut.isPending ? 'Adding…' : 'Add'}
            </button>
          </div>
        </div>
      )}

      {products.length === 0 ? (
        <p className="text-sm text-slate-400 italic m-0" data-testid="products-empty">
          No products tracked yet.
        </p>
      ) : (
        <>
          <div className="divide-y divide-slate-100 border border-slate-200 rounded-lg overflow-hidden">
            {products.map((p) => (
              <div key={p.id} data-testid={`product-row-${p.id}`}
                className="flex items-center gap-3 px-3 py-2 text-sm bg-white">
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-900 truncate">{p.product_name}</div>
                  {p.notes && <div className="text-xs text-slate-500">{p.notes}</div>}
                </div>
                <input
                  type="number" min="0.01"
                  defaultValue={p.quantity}
                  onBlur={(e) => {
                    const v = Number(e.target.value);
                    if (v > 0 && v !== p.quantity) qtyMut.mutate({ pid: p.id, quantity: v });
                  }}
                  data-testid={`product-qty-${p.id}`}
                  className="w-16 px-2 py-1 border border-slate-200 rounded text-xs text-right"
                  title="Quantity"
                />
                <span className="text-xs text-slate-500 w-20 text-right">
                  {p.unit_price != null ? fmtAmount(p.unit_price) : '—'}
                </span>
                <span className="text-xs font-semibold text-slate-700 w-20 text-right">
                  {p.line_total != null ? fmtAmount(p.line_total) : '—'}
                </span>
                <button
                  type="button"
                  onClick={() => deleteMut.mutate(p.id)}
                  className="text-slate-300 hover:text-red-500 text-xs"
                  aria-label={`Delete ${p.product_name}`}
                >✕</button>
              </div>
            ))}
          </div>
          <div className="flex justify-between items-center mt-2 text-xs text-slate-500">
            <span data-testid="products-total">
              Products total: <strong className="text-slate-700">{fmtAmount(productsTotal)}</strong>
            </span>
            {dealAmount != null && productsTotal > 0 && productsTotal !== dealAmount && (
              <span className="text-amber-600">
                deal amount is {fmtAmount(dealAmount)} — consider syncing
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}


function DocumentsCard({ oppId }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const fileRef = useRef(null);
  const queryKey = ['crm-documents', oppId];

  const { data: docs = [] } = useQuery({ queryKey, queryFn: () => listDocuments(oppId) });

  const uploadMut = useMutation({
    mutationFn: (file) => uploadDocument(oppId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      toast.success('Document uploaded');
      if (fileRef.current) fileRef.current.value = '';
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Upload failed'),
  });

  const deleteMut = useMutation({
    mutationFn: (docId) => deleteDocument(docId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey }),
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to delete'),
  });

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4" data-testid="documents-card">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
          Documents
        </span>
        <label className="px-2.5 py-1 text-xs font-medium bg-brand-600 hover:bg-brand-700 text-white rounded-md cursor-pointer">
          {uploadMut.isPending ? 'Uploading…' : '⬆ Upload'}
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            data-testid="document-file-input"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) uploadMut.mutate(f);
            }}
          />
        </label>
      </div>
      <p className="text-xs text-slate-400 mt-0 mb-2">
        Proposals, contracts, quotes — up to 10MB each.
      </p>
      {docs.length === 0 ? (
        <p className="text-sm text-slate-400 italic m-0" data-testid="documents-empty">
          No documents yet.
        </p>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-lg overflow-hidden">
          {docs.map((d) => (
            <div key={d.id} data-testid={`document-row-${d.id}`}
              className="flex items-center gap-3 px-3 py-2 text-sm bg-white">
              <span>📄</span>
              <div className="flex-1 min-w-0">
                <a
                  href={documentDownloadUrl(d.id)}
                  className="font-medium text-brand-600 hover:underline truncate block"
                  data-testid={`document-download-${d.id}`}
                >
                  {d.filename}
                </a>
                <div className="text-xs text-slate-400">
                  {fmtBytes(d.size_bytes)} · {new Date(d.uploaded_at).toLocaleDateString()}
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (confirm(`Delete "${d.filename}"?`)) deleteMut.mutate(d.id);
                }}
                className="text-slate-300 hover:text-red-500 text-xs"
                aria-label={`Delete ${d.filename}`}
              >✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
