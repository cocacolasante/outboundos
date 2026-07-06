import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  closestCorners,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

import {
  createOpportunity,
  getDefaultPipeline,
  getPipelineSummary,
  listOpportunities,
  updateOpportunity,
} from '../api/crm.js';
import { useToast } from '../components/Toast.jsx';
import useReducedMotion from '../utils/useReducedMotion.js';
import { formatCurrency, formatDate as fmtDate } from '../utils/format.js';
import { Button } from '../components/ui.jsx';
import { Skeleton } from '../components/states.jsx';

// Pipeline order fallback — matches the backend OpportunityStage enum.  The
// board prefers the CONFIGURABLE stages from /crm/pipelines/default; this is
// the back-compat fallback when no pipeline is seeded.
export const STAGES = [
  { value: 'prospecting', label: 'Prospecting' },
  { value: 'qualification', label: 'Qualification' },
  { value: 'proposal', label: 'Proposal' },
  { value: 'negotiation', label: 'Negotiation' },
  { value: 'closed_won', label: 'Closed won' },
  { value: 'closed_lost', label: 'Closed lost' },
];

const STAGE_HEADER_CLASS = {
  prospecting: 'border-sky-300 text-sky-700',
  qualification: 'border-amber-300 text-amber-700',
  proposal: 'border-orange-300 text-orange-700',
  negotiation: 'border-violet-300 text-violet-700',
  closed_won: 'border-emerald-300 text-emerald-700',
  closed_lost: 'border-slate-300 text-slate-500',
};

// Re-exported from the shared formatter so existing importers (Reports.jsx)
// keep working while the definition lives in one place.
export const fmtAmount = formatCurrency;

/**
 * Resolve which stage a drop landed on.  ``over.id`` is either a column id
 * (a stage key) or a card id (an opportunity) — in the latter case we map to
 * that card's current stage.  Pure + exported for unit testing the drag logic
 * without simulating native drag events.
 */
export function resolveDropTarget(overId, stageKeys, cardStage) {
  if (overId == null) return null;
  if (stageKeys.includes(overId)) return overId;
  return cardStage[overId] ?? null;
}

function cardSubtitle(o) {
  return (
    o.company
    || [o.first_name, o.last_name].filter(Boolean).join(' ')
    || o.email
    || '—'
  );
}


// --------------------------------------------------------------------------
// Draggable card
// --------------------------------------------------------------------------

function OpportunityCard({ opp, onOpen, reducedMotion }) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: opp.id });

  const style = {
    transform: CSS.Translate.toString(transform),
    transition: reducedMotion ? 'none' : transition,
    opacity: isDragging ? 0.4 : 1,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      tabIndex={0}
      data-testid={`opp-card-${opp.id}`}
      aria-label={`${opp.name}, ${fmtAmount(opp.amount)}. Press space to pick up and move between stages; enter to open.`}
      onClick={() => onOpen(opp.id)}
      onKeyDown={(e) => {
        // Enter opens; Space is reserved by dnd-kit for pick-up.
        if (e.key === 'Enter') { e.preventDefault(); onOpen(opp.id); }
      }}
      className="w-full text-left bg-white rounded-lg border border-slate-200 shadow-sm p-3
                 cursor-grab active:cursor-grabbing hover:border-brand-300 hover:shadow
                 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
    >
      <div className="text-sm font-medium text-slate-900 truncate">{opp.name}</div>
      <div className="text-xs text-slate-500 mt-0.5 truncate">{cardSubtitle(opp)}</div>
      <div className="flex items-center justify-between mt-1.5 text-xs">
        <span className="font-semibold tabular-nums text-slate-700">{fmtAmount(opp.amount)}</span>
        <span className="text-slate-400">{opp.close_date ? fmtDate(opp.close_date) : ''}</span>
      </div>
      {opp.open_task_count > 0 && (
        <div className="mt-1 text-xs text-amber-600">
          ☑️ {opp.open_task_count} open task{opp.open_task_count === 1 ? '' : 's'}
        </div>
      )}
    </div>
  );
}


// --------------------------------------------------------------------------
// Droppable stage column
// --------------------------------------------------------------------------

function StageColumn({ column, cards, onOpen, reducedMotion }) {
  const { setNodeRef, isOver } = useDroppable({ id: column.key });
  const count = cards.length;
  const total = cards.reduce((sum, c) => sum + (c.amount || 0), 0);

  return (
    <div
      ref={setNodeRef}
      data-testid={`stage-column-${column.key}`}
      role="group"
      aria-label={`${column.label} stage, ${count} deal${count === 1 ? '' : 's'}, ${fmtAmount(total)}`}
      className={`flex-1 min-w-[230px] rounded-card border p-2.5 transition-colors duration-fast
        ${isOver ? 'bg-brand-50 border-brand-300' : 'bg-slate-50 border-slate-200'}`}
    >
      <div className={`flex items-baseline justify-between px-1.5 pb-2 mb-2 border-b-2
        ${STAGE_HEADER_CLASS[column.key] || 'border-slate-300 text-slate-600'}`}>
        <span className="text-sm font-semibold">{column.label}</span>
        <span className="text-xs text-slate-400 tabular-nums" data-testid={`stage-total-${column.key}`}>
          {count} · {fmtAmount(total)}
        </span>
      </div>
      <SortableContext items={cards.map((c) => c.id)} strategy={verticalListSortingStrategy}>
        <div className="space-y-2 min-h-[40px]">
          {count === 0 ? (
            <div className="text-xs text-slate-400 italic px-1.5 py-3 text-center">empty</div>
          ) : cards.map((o) => (
            <OpportunityCard key={o.id} opp={o} onOpen={onOpen} reducedMotion={reducedMotion} />
          ))}
        </div>
      </SortableContext>
    </div>
  );
}


// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export default function Opportunities() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const reducedMotion = useReducedMotion();

  const [creating, setCreating] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const [view, setView] = useState('board'); // 'board' | 'list'
  const [activeId, setActiveId] = useState(null);

  const OPPS_KEY = ['crm-opportunities'];
  const { data: page, isLoading } = useQuery({
    queryKey: OPPS_KEY,
    queryFn: () => listOpportunities({ page_size: 500 }),
  });
  // Configurable stages drive the columns; fall back to the enum order.
  const { data: pipeline } = useQuery({
    queryKey: ['crm-default-pipeline'],
    queryFn: getDefaultPipeline,
    retry: false,
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const opps = page?.items ?? [];
  const oppById = useMemo(
    () => Object.fromEntries(opps.map((o) => [o.id, o])),
    [opps],
  );

  const allColumns = useMemo(() => {
    if (pipeline?.stages?.length) {
      return pipeline.stages.map((s) => ({
        key: s.key, label: s.name, closed: s.is_won || s.is_lost,
      }));
    }
    return STAGES.map((s) => ({
      key: s.value, label: s.label, closed: s.value.startsWith('closed_'),
    }));
  }, [pipeline]);

  const columns = showClosed ? allColumns : allColumns.filter((c) => !c.closed);
  const stageKeys = allColumns.map((c) => c.key);

  const byStage = {};
  for (const c of allColumns) byStage[c.key] = [];
  for (const o of opps) (byStage[o.stage] ||= []).push(o);

  const cardStage = useMemo(
    () => Object.fromEntries(opps.map((o) => [o.id, o.stage])),
    [opps],
  );

  // Optimistic stage move with graceful rollback.
  const moveMutation = useMutation({
    mutationFn: ({ id, stage }) => updateOpportunity(id, { stage }),
    onMutate: async ({ id, stage }) => {
      await queryClient.cancelQueries({ queryKey: OPPS_KEY });
      const prev = queryClient.getQueryData(OPPS_KEY);
      queryClient.setQueryData(OPPS_KEY, (old) => {
        if (!old?.items) return old;
        return {
          ...old,
          items: old.items.map((o) => (o.id === id ? { ...o, stage } : o)),
        };
      });
      return { prev };
    },
    onError: (err, _vars, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(OPPS_KEY, ctx.prev); // roll back
      toast.error(err?.response?.data?.detail || 'Could not move the deal — reverted');
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: OPPS_KEY });
      queryClient.invalidateQueries({ queryKey: ['crm-pipeline'] });
    },
  });

  function onDragEnd(event) {
    const { active, over } = event;
    setActiveId(null);
    if (!over) return;
    const target = resolveDropTarget(over.id, stageKeys, cardStage);
    const opp = oppById[active.id];
    if (!target || !opp || opp.stage === target) return;
    moveMutation.mutate({ id: active.id, stage: target });
  }

  const activeOpp = activeId ? oppById[activeId] : null;

  // Screen-reader announcements for keyboard drag (space to pick up, arrows to
  // move, space to drop) — names the deal + the stage it's over / landed on.
  const stageLabelByKey = Object.fromEntries(allColumns.map((c) => [c.key, c.label]));
  const dealName = (id) => oppById[id]?.name || 'deal';
  const announcements = {
    onDragStart: ({ active }) => `Picked up ${dealName(active.id)}.`,
    onDragOver: ({ active, over }) => {
      const t = over ? resolveDropTarget(over.id, stageKeys, cardStage) : null;
      return t ? `${dealName(active.id)} is over ${stageLabelByKey[t] || t}.` : '';
    },
    onDragEnd: ({ active, over }) => {
      const t = over ? resolveDropTarget(over.id, stageKeys, cardStage) : null;
      return t ? `Moved ${dealName(active.id)} to ${stageLabelByKey[t] || t}.` : `${dealName(active.id)} returned.`;
    },
    onDragCancel: ({ active }) => `Cancelled moving ${dealName(active.id)}.`,
  };

  return (
    <div className="p-6 max-w-[110rem] mx-auto">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Opportunities</h1>
        <div className="flex items-center gap-3">
          {/* View toggle */}
          <div className="inline-flex rounded-lg border border-slate-300 overflow-hidden" role="group" aria-label="View">
            {['board', 'list'].map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                data-testid={`view-${v}`}
                aria-pressed={view === v}
                className={`px-3 py-1.5 text-sm font-medium ${
                  view === v ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                {v === 'board' ? 'Board' : 'List'}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={showClosed}
              onChange={(e) => setShowClosed(e.target.checked)}
              data-testid="show-closed-toggle"
            />
            Show closed
          </label>
          <Button onClick={() => setCreating(true)} data-testid="new-opportunity-btn">
            + New opportunity
          </Button>
        </div>
      </div>
      <p className="text-sm text-slate-500 mb-5">
        Your deal pipeline. Drag a card between stages to move the deal (the
        change is logged). Click a card for details + activity log.
      </p>

      {isLoading ? (
        <div className="flex gap-3 overflow-x-auto pb-4" data-testid="board-loading">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="flex-1 min-w-[230px] bg-slate-50 rounded-card border border-slate-200 p-2.5 space-y-2">
              <Skeleton className="h-4 w-1/2 mb-2" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ))}
        </div>
      ) : view === 'list' ? (
        <OpportunityList opps={opps} columns={allColumns} onOpen={(id) => navigate(`/opportunities/${id}`)} showClosed={showClosed} />
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCorners}
          accessibility={{ announcements }}
          onDragStart={(e) => setActiveId(e.active.id)}
          onDragCancel={() => setActiveId(null)}
          onDragEnd={onDragEnd}
        >
          <div className="flex gap-3 overflow-x-auto pb-4" data-testid="pipeline-board">
            {columns.map((column) => (
              <StageColumn
                key={column.key}
                column={column}
                cards={byStage[column.key] || []}
                onOpen={(id) => navigate(`/opportunities/${id}`)}
                reducedMotion={reducedMotion}
              />
            ))}
          </div>
          <DragOverlay dropAnimation={reducedMotion ? null : undefined}>
            {activeOpp ? (
              <div className="bg-white rounded-lg border border-brand-300 shadow-lg p-3 w-[230px] rotate-2">
                <div className="text-sm font-medium text-slate-900 truncate">{activeOpp.name}</div>
                <div className="text-xs text-slate-500 mt-0.5 truncate">{cardSubtitle(activeOpp)}</div>
                <div className="mt-1.5 text-xs font-semibold tabular-nums text-slate-700">{fmtAmount(activeOpp.amount)}</div>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      )}

      {creating && <NewOpportunityModal onClose={() => setCreating(false)} />}
    </div>
  );
}


// --------------------------------------------------------------------------
// List (table) view — alternative to the board
// --------------------------------------------------------------------------

function OpportunityList({ opps, columns, onOpen, showClosed }) {
  const labelByKey = Object.fromEntries(columns.map((c) => [c.key, c.label]));
  const closedKeys = new Set(columns.filter((c) => c.closed).map((c) => c.key));
  const rows = showClosed ? opps : opps.filter((o) => !closedKeys.has(o.stage));

  if (rows.length === 0) {
    return <div className="text-center text-slate-400 py-12" data-testid="list-empty">No opportunities.</div>;
  }
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200" data-testid="opportunity-list">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-50 border-b border-slate-200">
            {['Name', 'Stage', 'Company', 'Amount', 'Close date'].map((h) => (
              <th key={h} className={`px-4 py-2.5 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider ${h === 'Amount' ? 'text-right' : ''}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => (
            <tr
              key={o.id}
              onClick={() => onOpen(o.id)}
              data-testid={`opp-row-${o.id}`}
              className="border-b border-slate-100 last:border-0 hover:bg-slate-50 cursor-pointer"
            >
              <td className="px-4 py-2.5 font-medium text-slate-900">{o.name}</td>
              <td className="px-4 py-2.5 text-slate-600">{labelByKey[o.stage] || o.stage}</td>
              <td className="px-4 py-2.5 text-slate-600">{cardSubtitle(o)}</td>
              <td className="px-4 py-2.5 text-right tabular-nums text-slate-700">{fmtAmount(o.amount)}</td>
              <td className="px-4 py-2.5 text-slate-500">{o.close_date ? fmtDate(o.close_date) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function NewOpportunityModal({ onClose }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    name: '', amount: '', close_date: '', company: '',
    first_name: '', last_name: '', email: '',
  });
  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const createMut = useMutation({
    mutationFn: () => createOpportunity({
      name: form.name.trim(),
      amount: form.amount === '' ? null : Number(form.amount),
      close_date: form.close_date || null,
      company: form.company.trim() || null,
      first_name: form.first_name.trim() || null,
      last_name: form.last_name.trim() || null,
      email: form.email.trim() || null,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['crm-opportunities'] });
      queryClient.invalidateQueries({ queryKey: ['crm-pipeline'] });
      toast.success('Opportunity created');
      onClose();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Failed to create'),
  });

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6"
        onClick={(e) => e.stopPropagation()}
        data-testid="new-opportunity-modal"
      >
        <div className="flex justify-between items-center mb-4">
          <h2 className="m-0 text-lg font-semibold text-slate-900">New opportunity</h2>
          <button type="button" onClick={onClose} aria-label="Close"
            className="text-slate-400 hover:text-slate-600 text-xl bg-transparent border-none cursor-pointer p-1">×</button>
        </div>
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">Deal name *</label>
            <input type="text" value={form.name} onChange={(e) => update('name', e.target.value)}
              data-testid="new-opp-name"
              placeholder="Acme — managed IT contract"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">Amount ($)</label>
              <input type="number" min="0" value={form.amount}
                onChange={(e) => update('amount', e.target.value)}
                data-testid="new-opp-amount"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">Expected close</label>
              <input type="date" value={form.close_date}
                onChange={(e) => update('close_date', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">Company</label>
              <input type="text" value={form.company} onChange={(e) => update('company', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">Contact email</label>
              <input type="text" value={form.email} onChange={(e) => update('email', e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={onClose}>Cancel</Button>
            <Button
              size="sm"
              onClick={() => createMut.mutate()}
              disabled={!form.name.trim()}
              loading={createMut.isPending}
              data-testid="new-opp-save"
            >
              {createMut.isPending ? 'Creating…' : 'Create'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
