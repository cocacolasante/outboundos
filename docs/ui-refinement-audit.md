# UI refinement — Phase 0 audit & token proposal

_An "Apple-feel" presentation-layer pass: calm, precise, tactile, consistent.
This documents the current state, the top consistency problems, and the
proposed token + primitive foundation._

> **Status:** Phase 0 (audit only). No styling changes yet. Awaiting approval
> before Phase 1. Functionality, routes, API calls, and data flow are
> untouched throughout this entire effort.

---

## 1. Detected stack & current foundation

- **Framework:** React 18 + Vite + React Router 6 + TanStack Query.
- **Styling:** **Tailwind CSS** (utility classes). No CSS Modules,
  styled-components, or MUI. Global CSS is `src/index.css` (just the three
  `@tailwind` directives + a 2-line reset + system font stack).
- **Shell:** `App.jsx` → `flex` layout with a dark `Nav.jsx` sidebar (collapsible,
  persisted to `localStorage`) + scrollable `<main>`; global `ToastProvider`,
  `ErrorBoundary`, `NotificationBell`.
- **Existing design seed** (from the recent CRM "Phase 5" work — the base we
  extend, _not_ replace):
  - `tailwind.config.js` `theme.extend`: `brand` (aliased to blue),
    `rounded-card` (12px) / `rounded-pill`, `shadow-card` / `shadow-card-hover`,
    `duration-fast` (120ms) / `duration-base` (200ms),
    `ease-spring` (`cubic-bezier(0.34,1.56,0.64,1)`).
  - `components/ui.jsx`: `Button` (primary/secondary/danger/ghost; sizes;
    loading + disabled + focus-visible), `Card`, `PageHeader`.
  - `components/states.jsx`: `Skeleton`, `LoadingCards`, `EmptyState`,
    `ErrorState` (reduced-motion via `motion-safe:`).
  - `components/charts.jsx`, `utils/format.js`, `utils/useReducedMotion.js`.
  - **Adoption is shallow** — used on ~3 CRM screens; the rest of the app
    hand-rolls everything.

## 2. Main surfaces & current state

| Surface | Notes |
|---|---|
| **Replies** (inbox triage) | reply cards + sentiment badges + convert/draft; **bare "Loading…"**, **no error state**. Defines the product feel. |
| **SequenceBuilder** (@xyflow canvas) | node palette + node cards + recursive condition editor; bare "Loading sequence…", no canvas-empty state, validation only surfaces on publish. |
| **CampaignDetail** | 4 tabs (Overview/Leads/Activity/Analytics), many inline editor cards; **bare "Loading…" ×4**, no error banners, toast-only success. |
| **Settings** | tabbed (inboxes / LinkedIn / API status / agent); bare loading + bare error strings; designed empty states. |
| **Leads + LeadTable** | search/filter + dense table + CRM modal; bare "Loading…", plain "No leads." empty, **no skeleton**, no error/retry. |
| Campaigns, Opportunities, Reports, ReportBuilder, Dashboard, Signals, Lookalikes, SocialRadar, ResearchClient, Analytics, Preview, CampaignCreate | mix of the patterns above. |

## 3. Top 10 consistency problems (with evidence)

1. **Accent not unified** — `bg-blue-600` ×57 vs `bg-brand-600` ×3; `focus:ring-blue-500` ×49 (token unused).
2. **Buttons hand-rolled** — 226 `<button>`s, only 14 use the shared `Button` (~94% hand-rolled, 10+ className patterns).
3. **8 hand-rolled modals** — `fixed inset-0 bg-black/50` duplicated; no shared Modal/Sheet; close affordance varies (`×` text vs icon).
4. **Inputs/selects/textareas** — ~50/49/20 instances, slightly-varied classes; no shared `Field`; label sizes drift (`text-sm` vs `text-xs`).
5. **3 hand-rolled tab strips** with active-color drift (`blue-600` vs `blue-700`).
6. **Status/badge colors duplicated** across 6+ files with divergent semantics (Leads `STATUS_CLASSES`/`CRM_STATUS_CLASSES`, Analytics `SEQ_KIND_COLORS`, CampaignDetail `PILL`).
7. **Radius drift** — `rounded-lg` ×308, `rounded` ×103, `rounded-xl` ×80, `rounded-md` ×63, `rounded-full` ×41, `rounded-card` ×4.
8. **Shadow drift** — `shadow-sm` ×68, `shadow-2xl` ×10 (modals), `shadow-lg` ×3, `shadow-card` ×2.
9. **No designed loading/error** — zero skeletons; bare `"Loading…"` strings everywhere; error states largely absent.
10. **No `tabular-nums`** anywhere (0 instances) despite many metric/number tables; mixed label sizes + spacing off the 8pt rhythm in places.

## 4. Proposed token foundation (additive; stays in Tailwind)

- **Color:** keep `brand` (blue `#2563eb`) as the single accent. Add semantic
  scales `success` (emerald), `warning` (amber), `danger` (red), `info` (sky);
  neutral stays `slate`. A `utils/statusColors.js` module collapses the 6+
  divergent status→class maps into one semantic source.
- **Type (~6 roles):** `display` / `title` / `heading` / `body` / `label` /
  `caption` — documented + a base-layer body default with
  `-webkit-font-smoothing: antialiased`.
- **Spacing:** 8pt grid (Tailwind's 4px scale already is) — enforced via the
  primitives + the sweep.
- **Radius:** `rounded-card` (cards/sheets), `rounded-lg` (controls),
  `rounded-pill`/`-full` (pills). Retire stray `rounded`/`rounded-md`/`-2xl`.
- **Shadow:** `shadow-card` / `shadow-card-hover` + new `shadow-overlay`
  (modals/dropdowns) to retire `shadow-2xl`.
- **Motion:** `framer-motion` spring presets in `utils/motion.js`; keep
  `duration-fast/base` + `ease-spring` for CSS fallbacks; everything gated by
  `useReducedMotion()` + `prefers-reduced-motion`.
- **Base layer (`index.css`):** global `:focus-visible` ring (brand),
  `::selection` (brand-100), antialiasing, a `.tabular` helper.

## 5. Primitive library (extend `components/ui.jsx` → `components/ui/` barrel)

Keep `components/ui.jsx` as the public barrel so existing imports don't break.
**Have:** Button, Card, PageHeader, Skeleton/LoadingCards/EmptyState/ErrorState,
Toast, MetricsGrid, charts. **Add:** Input, Select, Textarea, Field, Checkbox,
Toggle, Radio, **Modal/Sheet** (consolidate the 8 hand-rolled), Dropdown/Menu,
Tooltip, **Badge** (+ semantic status variants), **Tabs** (one accessible strip),
**Table** (dense, hairline rows, sticky header, sortable, `tabular-nums`).
A **`/ui-kit`** route renders every primitive in all states.

## 6. Phase plan & rough effort

| Phase | Scope | Effort |
|---|---|---|
| **0** | This audit | — |
| **1** | Tokens + base layer; add `framer-motion`, `utils/motion.js`, `utils/statusColors.js`; retrofit Replies as proof | M |
| **2** | Full primitive library + `/ui-kit` route; consolidate duplicates | L |
| **3** | App shell + hard screens first (SequenceBuilder, Replies, CampaignDetail), then all ~18 pages | L |
| **4** | framer-motion springs (mounts/modals/dropdowns/tabs/list reorder) + reduced-motion | M |
| **5** | Designed states on every view; form validation; `Table` adoption (sticky/sortable/tabular) | L |
| **6** | A11y (AA contrast, focus, keyboard, ARIA) + responsive + final consistency hunt + QA report | M |

## 7. Confirmed decisions

- **Motion:** add `framer-motion` (the only new dependency).
- **`/ui-kit`** internal preview route: yes.
- **Accent:** keep the existing blue.
- **Breadth:** every page, equal depth.

## 8. Guardrails

Presentation layer only — no routing/state/data/API refactors, no styling
framework migration, no new deps beyond `framer-motion`. One phase per
checkpoint with reviewable diffs; the **408** existing frontend tests stay
green after each phase (class-based assertions swapped to semantic ones). When
in doubt, remove rather than add.
