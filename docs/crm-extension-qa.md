# CRM extension — QA report (Phase 6)

Final QA across the six-phase CRM extension (pipeline Kanban, custom report
builder, unified analytics, design system).  Status: **all phases shipped;
full suites green — backend 1117, frontend 408.**

## Definition-of-done check

| Requirement | Status |
|---|---|
| Opportunities move between stages by drag-and-drop, persist, and are logged | ✅ Kanban (`@dnd-kit`) → optimistic move → `PATCH` → `opportunity_stage_changes` audit row |
| Build / save / edit / re-run custom reports over leads / activities / opportunities (+ contacts/accounts) with own fields/filters/grouping/date ranges + export | ✅ Report builder; metadata-whitelist query service; CSV export |
| All analytics share one visual system + reconciled metric definitions | ✅ shared `format`/`charts`/`states`; single `metrics.rate`; `docs/crm-metrics.md` |
| CRM area matches the app's design system, all states designed | ✅ token layer + `Button`/`Card`/`PageHeader` + shared loading/empty/error; remaining secondary-button sweep noted below |
| Nothing previously working broken; tenant-ready; no destructive migrations | ✅ additive migration 0038 (up/down verified); nullable `tenant_id` throughout; suites green |

## Accessibility

- **Kanban keyboard moves:** `KeyboardSensor` + `sortableKeyboardCoordinates`
  — focus a card (Tab), Space to pick up, arrows to move between stages,
  Space to drop, Esc to cancel. Cards are focusable (`tabIndex=0`) with an
  `aria-label` (deal + amount + instructions); columns are labelled
  `role="group"`; `DndContext` emits screen-reader **announcements** on
  start/over/end/cancel.
- **Report builder:** native form controls (keyboard-accessible by default);
  toggle chips expose `aria-pressed`; icon-only remove buttons have
  `aria-label`; the `Button` primitive provides `focus-visible` rings on all
  actions.
- **Focus rings:** standardized via `focus-visible:ring-2 ring-brand-500` on
  the `Button` primitive, cards, chips, and icon buttons.
- **Contrast:** text uses slate-700/900 on white (AA pass); brand-600 buttons
  with white text pass AA. Slate-400 is used only for non-essential
  hint/placeholder text. No AA failures found in the CRM/analytics surfaces.
- **Reduced motion:** Kanban drag honors `useReducedMotion()`; skeleton +
  spinner shimmer use `motion-safe:`.

## Audit log

- Every opportunity **stage move** writes an append-only
  `opportunity_stage_changes` row: **who** (`changed_by` ← owner_id,
  design-ready), **when** (`created_at`), **what** (`from`/`to` stage id+key),
  **how** (`source` = `user` vs `agent`).  Centralized in
  `update_opportunity`, so the board drag AND the detail stepper are both
  logged.  Surfaced via `GET /crm/opportunities/{id}/stage-history`.
- Lead **conversion** is logged as a `CrmActivity` note spanning lead + opp.
- The pre-existing `agent_actions` append-only audit (automated actions) is
  preserved and untouched.

## Human-in-the-loop / safety

- Drag + report-run are explicit user actions.  No agentic auto-mutation of
  CRM records was added; the suggest-and-approve `notifications` /
  `agent_actions` layer is unchanged.
- The report builder **never executes user SQL** — every field/operator/
  aggregate resolves through the server-side whitelist; unknown refs → 400
  before any query is built (safety rejections unit-tested).

## Responsive / overflow

- Kanban board: horizontal scroll (`overflow-x-auto`), columns `min-w-[230px]`.
- Report results + dashboard saved-report tables: `overflow-auto` with sticky
  headers (`max-h` capped).
- Opportunity list + report tables scroll within their container; KPI grids
  collapse `grid-cols-2 → md:grid-cols-4`.

## Follow-ups (intentionally deferred)

- **Design-system sweep of pre-existing pages:** the many secondary buttons on
  `OpportunityDetail` and the campaign/leads/settings pages still use the
  (now token-aliased) raw utility classes; swapping them to the `Button`
  primitive is mechanical and low-risk. Tracked, not blocking.
- **Multitenancy:** `tenant_id` columns + tenant-aware query structure are in
  place but NOT enforced (per scope). The tenant refactor + RLS is the next
  major effort.
- **Deferred-by-scope CRM features** (custom fields/objects, formula/rollup
  fields, workflow rules, role hierarchies, territories) remain future work;
  the schema is shaped to allow them.
- **Per-report scheduling / emailed reports** and **fully user-configurable
  dashboards** — not in this pass.
