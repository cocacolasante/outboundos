# UI refinement — Phase 6 QA report

Closes the six-phase "Apple-feel" UI refinement (presentation layer only).
Companion to [`ui-refinement-audit.md`](ui-refinement-audit.md) (the Phase-0
audit + token proposal). This report records what shipped, the accessibility
and consistency status, and the prioritized follow-ups that were deliberately
left for a later mechanical pass.

_Verified at close: **frontend 423 tests passing**, `npm run build` clean,
backend unchanged (**1117**)._

---

## What shipped (by phase)

| Phase | Deliverable | State |
|---|---|---|
| 0 | Audit + token proposal (`ui-refinement-audit.md`) | ✅ |
| 1 | Token foundation (`tailwind.config.js` + `index.css` base layer), `framer-motion` + `utils/motion.js` + `utils/statusColors.js`; Replies retrofit | ✅ |
| 2 | Primitive library (`components/ui/*` → `ui.jsx` barrel) + `/ui-kit` preview | ✅ |
| 3 | Shell/IA: `Tabs` consolidation (CampaignDetail/Settings/SocialRadar), `PageHeader` scaffolding | ✅ |
| 4 | Motion: root `MotionConfig reducedMotion="user"`, page-mount transition, sliding tab indicator | ✅ |
| 5 | States (skeleton/empty/error) on every bare primary view, `Field` inline-validation, tabular numerics | ✅ |
| 6 | A11y + final consistency hunt + this QA report | ✅ |

## Phase 6 work

### Accent fully tokenized (the dominant audit finding)

The audit flagged the accent split between a one-off `blue-*` palette and the
`brand` token (`bg-blue-600` ×57, `focus:ring-blue-500` ×49, `brand` essentially
unused). The `brand` scale is a **hex-exact alias** of Tailwind's blue
(`50`→`900`, after adding `brand-900 #1e3a8a` this phase), so a global
`blue-*` → `brand-*` rename is a **pure visual no-op** that unifies the token.

- **0** `blue-*` class usages remain in non-test source (was ~330 occurrences
  across 25 files). No test asserted a `blue-*` class, so nothing was repointed.
- Sweep verified by `npm run build` (Tailwind compiles every `brand-*` shade)
  and the full 423-test suite staying green.

### Accessibility status

- **Global keyboard focus ring** — `:focus-visible` brand ring in the
  `index.css` base layer covers every focusable element; per-element rings were
  tokenized to `brand` and de-duplicated against the global one.
- **Reduced motion** — `<MotionConfig reducedMotion="user">` at the app root
  makes every framer-motion surface honor `prefers-reduced-motion`; the
  `states` skeletons gate `animate-pulse` behind `motion-safe:`.
- **Custom-control ARIA** — the primitives carry it: `Tabs` (roving tabindex +
  arrows, `role=tablist/tab`, `aria-selected`), `Menu` (`role=menu`, Esc +
  click-outside), `Modal` (`role=dialog`/`aria-modal`, focus trap + restore,
  Esc), `Toggle` (`role=switch`), `Tooltip` (hover + focus, `role=tooltip`),
  `Table` (`aria-sort`), `Field` (wires `htmlFor` + `aria-describedby` +
  `aria-invalid`, inline error `role=alert`).
- **Labeled controls** — every icon-only close button (`×`/`✕`) carries an
  `aria-label`; form inputs are label-associated; decorative icons are
  `aria-hidden`.
- **Contrast** — text/UI run on slate-700/900 on white and white on
  `brand-600` (#2563eb), both ≥ WCAG AA.

### Data density

`.tabular` (tabular-nums) applied to the metric tables (Reports deals +
`SimpleTable` summaries + Analytics funnels; Dashboard already had it) so
figures align in columns.

### Responsive / overflow

Page bodies use capped max-widths (`max-w-7xl`/`max-w-3xl`) with `mx-auto`;
wide data tables are wrapped in `overflow-x-auto` (Reports detail tables, the
`Table` primitive's own `overflow-auto`). The Kanban board scrolls
horizontally on narrow viewports via `@dnd-kit`'s column layout.

---

## Deferred follow-ups (mechanical; tracked, not blocking)

These were scoped out of the phased pass on purpose — each is a large, mostly
mechanical migration with its own test-contract risk, best done as a focused
follow-up rather than smuggled into a checkpoint. None changes behavior today;
all current screens work and are tokenized.

1. **Button-primitive fan-out** — 218 hand-rolled `<button>` vs 24 `<Button>`.
   The interaction matrix (hover/focus-visible/active/disabled/loading) already
   lives in the `Button` primitive; the remaining buttons follow the
   now-tokenized conventions, so this is a drop-in swap per call site.
2. **Modal consolidation** — 7 hand-rolled `fixed inset-0 bg-black/50` dialogs
   (ConnectInboxModal, ConnectLinkedInModal, LeadTable, Opportunities, Leads,
   SocialRadar, CampaignDetail) still lack the `Modal` primitive's focus trap +
   Esc/backdrop close. **This is the highest-value a11y follow-up** — migrating
   them to `Modal`/`Sheet` closes the keyboard-trap gap in one move. They were
   tokenized (`rounded-card` + `shadow-overlay`) where touched.
3. **`Table`-primitive migration for interactive tables** — Leads / `LeadTable`
   / contacts carry per-row checkboxes, row-click modals, and per-id testids
   (`lead-row-<id>`). A clean migration needs a small `rowTestId`/`rowProps`
   extension on the `Table` primitive (so the per-id testids survive) rather
   than test churn. Data-density was applied in place in the meantime.
4. **Semantic status palette** — `utils/statusColors.js` is the single source
   for status tones, but several pages still inline status tints (and the
   accent rename folded info-blue badges into `brand`). Adopting `info` (sky)
   for informational badges via `chipClasses` is a semantics refinement.
5. **Micro-consistency** — residual `shadow-2xl` (7 files) → `shadow-overlay`,
   `rounded-2xl` (10) → `rounded-card`, and `focus:` → `focus-visible:` on the
   per-element rings. Cosmetic; the global focus-visible ring already covers
   keyboard users.

## Definition of done (brief) — check

- ✅ Calm, precise, tactile, consistent — unified accent token, spring motion,
  designed states, tabular figures.
- ✅ Presentation layer only — no routing/state/data/API changes; every screen
  behaves identically.
- ✅ Stayed in Tailwind; one new dep (`framer-motion`, approved).
- ✅ One phase per checkpoint, reviewable diffs, tests green at each boundary.
- ◻️ "Every primitive adopted on every surface" — the design system + tokens are
  app-wide; the button/modal/table call-site fan-out continues per the
  follow-ups above.
