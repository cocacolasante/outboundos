# Design system (Phase 5)

The app's UI was built on consistent Tailwind utility conventions but had **no
formal token layer**.  Phase 5 codifies those conventions into named tokens +
shared primitives so CRM/analytics surfaces stop hand-rolling one-off styles.

## Tokens (`frontend/tailwind.config.js`, additive)

| Token | Value | Use |
|---|---|---|
| `colors.brand.{50..700}` | the blue scale the app already used | `bg-brand-600`, `text-brand-700`, `ring-brand-500` (renders identically to the old `blue-*`) |
| `borderRadius.card` | `0.75rem` (= `rounded-xl`) | card corners (`rounded-card`) |
| `borderRadius.pill` | `9999px` | pills/badges |
| `boxShadow.card` / `card-hover` | soft / lifted | `shadow-card`, `shadow-card-hover` |
| `transitionDuration.fast` / `base` | 120ms / 200ms | `duration-fast` |
| `transitionTimingFunction.spring` | `cubic-bezier(0.34,1.56,0.64,1)` | `ease-spring` for drag/reflow |

Additive only — every existing utility class is unchanged.

## Primitives (`frontend/src/components/ui.jsx`)

- **`Button`** — the single button primitive.  Variants `primary` / `secondary`
  / `danger` / `ghost`, sizes `sm` / `md`.  Encodes the **full interaction
  matrix in one place**: hover, `focus-visible` ring, active, `disabled`
  (opacity + no pointer events), and `loading` (spinner + disabled +
  `aria-busy`).  Forwards `data-testid` / `onClick` / `type` so adoption is a
  drop-in for `<button>`.
- **`Card`** — `bg-white rounded-card border border-slate-200 shadow-card`.
- **`PageHeader`** — title + optional subtitle + right-aligned actions slot
  for consistent page scaffolding.

## Shared states (`frontend/src/components/states.jsx`, from Phase 4)

`Skeleton`, `LoadingCards`, `EmptyState`, `ErrorState` — every analytics/CRM
view uses these for loading / empty / error rather than ad-hoc markup.  The
skeleton shimmer is gated behind `motion-safe:` so it respects
`prefers-reduced-motion`.

## Shared formatting + charts

- `utils/format.js` — `formatNumber/Currency/Percent/Date/DateTime` (null → `—`).
- `components/charts.jsx` — `SimpleBarChart/LineChart/PieChart` (one palette).
- `utils/useReducedMotion.js` — hook driving reduced-motion in the Kanban DnD.

## Motion + reduced motion

Drag/drop/reflow on the Kanban use spring transforms via @dnd-kit, gated by
`useReducedMotion()`; the skeleton + spinner shimmer use `motion-safe:` so
they stop animating under `prefers-reduced-motion: reduce`.

## Adoption status

- **Fully adopted:** Opportunities (Kanban + list), Report builder, Dashboard
  — primitives + shared states + tokens.
- **On the shared data layer:** Reports + campaign Analytics use the shared
  formatters + charts (Phase 4).
- **Remaining (folded into Phase 6's final consistency sweep):** convert the
  many secondary buttons on the OpportunityDetail record page and the
  pre-existing campaign/leads/settings surfaces to the `Button` primitive.
  These already follow the (now token-aliased) conventions; the sweep swaps
  them to the primitive for the complete focus/active/loading treatment.
