# CRM Extension — Phase 0 Audit & Plan

_Salesforce-grade CRM extensions: drag-and-drop opportunity pipeline, custom
report builder, and a unified analytics layer._

> **Status:** Phase 0 (audit only). No schema or UI changes yet. Awaiting
> approval before Phase 1.

---

## 0. Executive summary

The app already has a **lite CRM** (migrations `0025`–`0027`): opportunities
with a stage pipeline, manually-logged activities, documents, products, and an
agent layer (notifications + append-only `agent_actions` audit). Three gaps
separate it from the three target features:

1. **Stages are a hard-coded `OpportunityStage` enum** — not configurable, no
   pipeline concept. The Kanban is **read-only** (no drag-and-drop), and stage
   changes are **not logged**.
2. **No custom report builder.** "Reports" today is a *fixed* CRM dashboard
   (canned KPIs/funnel) — no user-defined fields/filters/grouping, no saved
   definitions.
3. **Three independent reporting surfaces** (campaign analytics, CRM reports,
   sequence analytics) with **duplicated metric helpers** (`_rate` defined 3×
   on the backend, `pct`/`fmtAmount` re-defined per page) and no shared chart
   or empty/loading/error components.

Also absent and relevant: **no `Account`/`Contact` tables** (company/person are
denormalized onto `leads` + `crm_opportunities`), **no `owner`/users table**
(single-operator app, no auth), and **no `tenant_id`** anywhere.

None of this blocks the plan — the existing model is a clean base to extend
additively.

---

## 1. Current CRM data model

### 1.1 Tables & key fields

| Table | Purpose | Key fields | Relationships |
|---|---|---|---|
| `leads` (`models/lead.py`) | Doubles as the CRM lead AND the outreach recipient | `email?`, `first_name`, `last_name`, `company`, `company_website`, `job_title`, `campaign_id?` (nullable since 0025), `crm_status` (text: new/working/qualified/converted/unqualified), `converted_opportunity_id?`, research/compose/send status | `campaign_id → campaigns` (SET NULL semantics via app); `converted_opportunity_id → crm_opportunities` |
| `crm_opportunities` (`models/crm.py`) | A deal in the pipeline | `name`, `stage` (enum), `amount` Numeric(14,2)?, `close_date?`, `probability?` int, `description?`, `closed_at?`, `loss_reason?`, **contact snapshot** (`first_name`/`last_name`/`email`/`phone`/`company`/`job_title`/`linkedin_url`), `source_lead_id?` | `source_lead_id → leads (SET NULL)`; 1‑N `crm_activities`, `crm_documents`, `crm_opportunity_products` |
| `crm_activities` (`models/crm.py`) | Human-logged touch | `activity_type` (call/email/meeting/note/task), `subject`, `body?`, `direction?` (inbound/outbound), `due_at?`, `completed_at?`, `occurred_at`, agent fields (`reminder_sent_at?`, `sentiment?`, `is_agent_generated`) | `lead_id?` **OR** `opportunity_id?` (CHECK: ≥1 parent), both `ON DELETE CASCADE` |
| `crm_documents` | Opportunity file attachment (BYTEA, 10MB cap) | `filename`, `content_type`, `size_bytes`, `data` | `opportunity_id → crm_opportunities (CASCADE)` |
| `crm_opportunity_products` | Product-of-interest line item | `product_name`, `quantity`, `unit_price?`, `notes?` | `opportunity_id → crm_opportunities (CASCADE)` |
| `agent_actions` (`models/agent.py`) | **Append-only audit** of *automated* actions | `action_type`, `status`, `summary`, `detail` JSONB, `model?`, `cost_usd?`, optional `lead_id`/`opportunity_id`/`activity_id` | polymorphic-ish optional FKs |
| `notifications` (`models/agent.py`) | UI bell feed (suggest-and-approve surface) | `kind`, `title`, `body?`, `dedup_key` (unique), `read_at?`, `emailed_at?` | optional `lead_id`/`opportunity_id`/`activity_id` |

### 1.2 Relationship sketch (today)

```
campaigns ──< leads >── (converted) ──> crm_opportunities ──< crm_activities
                                   │                     ├──< crm_documents
                                   │                     └──< crm_opportunity_products
                       crm_activities >── leads  (activity attaches to lead OR opp)
agent_actions / notifications ──(optional)──> leads | opportunities | activities
```

### 1.3 Gaps vs. a Salesforce-style model

| Salesforce concept | Today | Gap |
|---|---|---|
| **Account** (company) | Denormalized `company` text on lead + opp | No `accounts` table; no account ↔ contacts ↔ opps graph |
| **Contact** (person) | `leads` + opp contact-snapshot columns | No `contacts` table distinct from leads; no account membership |
| **Opportunity** | `crm_opportunities` ✓ | Missing `owner`, `account_id`, `contact_id` FKs |
| **Stage / Pipeline** | Hard-coded `OpportunityStage` enum + `STAGE_DEFAULT_PROBABILITY` dict | Not configurable; no `pipelines`/`stages` tables; no per-stage won/lost flag as data |
| **Activity / Task** | `crm_activities` ✓ | Polymorphic link limited to lead/opp (no account/contact); no `owner` |
| **Owner / User** | None (no auth, single operator) | No `users`/`owner_id` — needed only as a *design-ready* nullable column |
| **Report / Dashboard** | Fixed CRM dashboard only | No saved, user-defined report definitions |
| **`tenant_id`** | None | Required (nullable) on every new table per constraints |

---

## 2. Opportunity stages & pipeline state

- **Storage:** pipeline state is the single `crm_opportunities.stage` column
  (`opportunity_stage` Postgres enum), indexed.
- **Configurability:** **none.** Stages, their order, default probabilities,
  and won/lost semantics are all **hard-coded in Python**
  (`models/crm.py`): `OpportunityStage` enum, `STAGE_DEFAULT_PROBABILITY`,
  `CLOSED_STAGES`. Order is implicit in enum declaration order.
- **Stage-change handling** (`routers/crm.py::update_opportunity`): sets
  `stage`, stamps/clears `closed_at`, clears `loss_reason` when leaving a
  closed stage, and auto-applies the default probability unless overridden.
  **It writes no audit/activity entry** — there is no record that a stage
  moved, who moved it, or when. (Conversion *is* logged as a `CrmActivity`
  note; stage moves are not.)
- **Board today** (`pages/Opportunities.jsx`): static columns per enum stage
  with per-column count + summed amount (`/crm/opportunities/pipeline`).
  Stage changes happen only via the `OpportunityDetail` page's stage stepper.
  **No drag-and-drop.**

---

## 3. Reporting / analytics surfaces (inventory)

| Surface | Backend | Frontend | Shows | Data source |
|---|---|---|---|---|
| **Campaign analytics** | `routers/analytics.py` `GET /campaigns/{id}/analytics` | `pages/Analytics.jsx` (recharts) | sent/delivered/open/click/reply/bounce/spam, rates, daily timeline, reputation score, research-quality breakdown, best subjects | `leads.send_status`, `email_events` |
| **Sequence performance** | `routers/sequences.py` `GET /campaigns/{id}/sequence/analytics` | card in `Analytics.jsx` (added recently) | per-node funnel (sent/skipped/failed/here) | `lead_step_executions` + entry-email `send_status` override |
| **CRM reports (fixed)** | `routers/reports.py` `GET /crm/reports/overview` `/deals` `/activities` | `pages/Reports.jsx` (recharts) | KPIs (won/lost, win rate, avg deal/cycle, open + weighted pipeline), won/lost monthly trend, pipeline-by-stage, forecast-by-close-month, loss reasons, activity breakdown, conversion funnel, deal/activity detail tables, CSV export | `crm_opportunities`, `crm_activities`, `leads` |
| **Campaign list rollups** | `routers/campaigns.py` (`_compute_stats_and_counts`) | `pages/Campaigns.jsx` / `CampaignDetail.jsx` | per-campaign send/open/reply counts | same as campaign analytics |

### 3.1 Inconsistencies found (Phase 4 targets)

- **Duplicated rate math:** `def _rate(numer, denom)` is defined **three
  times** — `analytics.py:34`, `campaigns.py:109`, `reports.py:105` — each its
  own copy. Risk of drift in how a "rate" rounds / handles divide-by-zero.
- **Duplicated frontend formatters:** `pct()` re-defined in both
  `Analytics.jsx` and `Reports.jsx`; `fmtAmount()` lives only in
  `Opportunities.jsx`; date formatting (`fmtDatetime`/`toLocaleString`) is
  ad-hoc per page. No shared `formatNumber`/`formatCurrency`/`formatPercent`.
- **No shared chart components / states:** each page wires recharts directly
  and rolls its own empty/loading/error markup. `MetricsGrid` is the *only*
  shared metric component.
- **Metric definitions not centralized:** "win rate", "weighted pipeline",
  "open rate", "reply rate" are each computed inline where used; weighting uses
  `STAGE_DEFAULT_PROBABILITY` in `reports.py` but that logic isn't reused
  elsewhere. These need a single documented definition.
- **No dashboard** combining CRM + campaign metrics (no `Dashboard.jsx`).

---

## 4. Frontend libraries (what's installed)

| Need | Installed | Verdict |
|---|---|---|
| **Charts** | `recharts ^2.15` (used in Analytics + Reports) | ✅ Reuse for report-builder charts + unified analytics. No new dep. |
| **Data fetching** | `@tanstack/react-query ^5.62` | ✅ Reuse. |
| **Flow/graph** | `@xyflow/react ^12.10` (SequenceBuilder only) | Not suited to Kanban. |
| **Tables** | None — all tables hand-rolled (`LeadTable`, Reports tables) | Hand-roll the report grid (sticky headers + tabular-nums) — **no new dep needed**. |
| **Drag-and-drop** | **None** | ⚠ **Decision required** (below). |
| **Motion / springs** | **None** (no framer-motion; no `prefers-reduced-motion` handling anywhere) | ⚠ **Decision required** (below). |
| **Design tokens** | **None formal** — `tailwind.config.js` `theme.extend` is empty; the "design system" is de-facto Tailwind utility conventions (slate/blue/emerald palette, `rounded-xl`, `border-slate-200`, `shadow-sm`, `text-2xl font-bold`) | Phase 5 will codify a light token layer in `theme.extend` + motion + a `useReducedMotion` hook. |

### 4.1 ⚠ Dependency flags (need your OK before adding)

**Drag-and-drop — recommend `@dnd-kit`** (`@dnd-kit/core`, `@dnd-kit/sortable`,
`@dnd-kit/utilities`, ~10KB gz):

- _Why:_ modern, maintained, **built-in keyboard sensor** (Phase 6 requires
  moving cards without a mouse), pointer/touch sensors, accessible
  drag announcements out of the box. Hand-rolling HTML5 DnD would not meet
  the keyboard/touch/a11y bar without significant custom work.
- _Alternative (no new dep):_ native HTML5 DnD — rejected: poor keyboard +
  touch support, conflicts with Phase 6.

**Spring motion — recommend NO new dependency.** Implement drag/drop/reflow
motion with CSS transforms + a spring-like `cubic-bezier` transition (or a tiny
local spring helper), gated by a `useReducedMotion` hook (matches
`(prefers-reduced-motion: reduce)`). Adding `framer-motion` is possible but not
necessary; **flagging it as optional** — say the word if you want true physics
springs and I'll add it.

> **Net new dependencies proposed: `@dnd-kit/core` + `@dnd-kit/sortable` +
> `@dnd-kit/utilities` only.** Everything else reuses what's installed.

---

## 5. Proposed extended data model

All new tables get a **nullable `tenant_id UUID`** (indexed) and are queried
through helpers that accept an optional tenant filter, so the future tenant
refactor + RLS drops in cleanly. All migrations additive & reversible; no
destructive changes to existing tables (only nullable column adds).

### 5.1 New tables

```
pipelines                      (id, tenant_id?, name, is_default, created_at, updated_at)
opportunity_stages             (id, tenant_id?, pipeline_id→pipelines, name, key,
                                 sort_order, default_probability, is_won, is_lost,
                                 is_active, created_at, updated_at)
accounts                       (id, tenant_id?, name, domain?, website?, industry?,
                                 size_hint?, created_at, updated_at)
contacts                       (id, tenant_id?, account_id?→accounts, first_name?,
                                 last_name?, email?, phone?, job_title?, linkedin_url?,
                                 source_lead_id?→leads, created_at, updated_at)
opportunity_stage_changes      (id, tenant_id?, opportunity_id→crm_opportunities,
                                 from_stage_id?, to_stage_id?, from_stage_key?,
                                 to_stage_key?, changed_by?, source(text:'user'|'agent'),
                                 note?, created_at)        ← stage-move audit
report_definitions             (id, tenant_id?, name, description?, data_source(text),
                                 definition JSONB, created_at, updated_at)
```

### 5.2 Additive columns on existing tables (all nullable / defaulted)

- `crm_opportunities`: `stage_id?` → `opportunity_stages`, `pipeline_id?` →
  `pipelines`, `account_id?` → `accounts`, `contact_id?` → `contacts`,
  `owner_id?` (UUID, no FK yet — users table doesn't exist), `tenant_id?`.
  **The existing `stage` enum column is kept and back-filled in lock-step**
  (dual-write) so all current reads/writes keep working unchanged; `stage_id`
  becomes the source of truth incrementally. No enum drop in this pass.
- `crm_activities`: `account_id?` → `accounts`, `contact_id?` → `contacts`,
  `owner_id?`, `tenant_id?` (keeps existing lead/opp polymorphic link).
- `leads`: `tenant_id?` (design-ready; leads stay the campaign recipient +
  CRM lead exactly as today).

### 5.3 Stage configurability strategy (back-compat)

- Seed one **default pipeline** + six **stages** mirroring today's enum
  (same keys, order, probabilities, won/lost flags) in the migration.
- `update_opportunity` and reporting resolve stage via `stage_id` when present,
  else fall back to the `stage` enum — so nothing breaks during/after rollout.
- `STAGE_DEFAULT_PROBABILITY` / `CLOSED_STAGES` become *seed data*, not the
  source of truth, but remain as the fallback for un-migrated rows.

### 5.4 `report_definitions.definition` JSON shape (metadata-driven; **no raw SQL**)

```json
{
  "object": "opportunities",
  "columns": ["name", "stage", "amount", "close_date", "account.name"],
  "filters": [
    {"field": "stage", "op": "in", "value": ["proposal", "negotiation"]},
    {"field": "amount", "op": "gte", "value": 5000},
    {"field": "close_date", "op": "relative_range", "value": "last_30_days"}
  ],
  "group_by": ["stage"],
  "aggregates": [{"field": "amount", "fn": "sum"}, {"fn": "count"}],
  "sort": [{"field": "amount", "dir": "desc"}],
  "limit": 1000
}
```

A server-side **whitelist registry** maps each `object` → allowed
fields (with type + the operators/aggregates legal for that type) and the
SQLAlchemy column/join it resolves to. The query service builds a
**parameterized** SQLAlchemy query from the definition, rejecting anything not
in the registry. Reportable objects v1: **leads, activities, opportunities,
contacts, accounts**.

### 5.5 Extended relationship sketch (proposed)

```
pipelines ──< opportunity_stages ──< crm_opportunities >── accounts
                                          │     │           └──< contacts
                                          │     └── contact_id ─> contacts
                                          ├──< opportunity_stage_changes  (audit)
                                          ├──< crm_activities (+ account_id/contact_id)
                                          ├──< crm_documents
                                          └──< crm_opportunity_products
report_definitions  (standalone; JSON definition resolved via whitelist registry)
```

---

## 6. Phase plan & rough effort

| Phase | Scope | Effort | Checkpoint deliverable |
|---|---|---|---|
| **0** | This audit | — | _(this doc — awaiting approval)_ |
| **1** | Schema foundation: pipelines/stages, accounts/contacts, activity FKs, owner/tenant columns, `opportunity_stage_changes`, `report_definitions`; additive migrations + models + seed default pipeline; backfill `stage_id`. **No UI.** | **M** (~1–1.5d) | Migrations + models + ERD/summary; up/down verified; full test suite green |
| **2** | Kanban DnD board (`@dnd-kit`): optimistic move → `PATCH` stage → `opportunity_stage_changes` audit row + `CrmActivity` note; per-column count+sum; list-view fallback; spring motion + `prefers-reduced-motion` | **M** (~1–1.5d) | Working board with persisted, logged stage changes + list view |
| **3** | Report builder: whitelist registry + metadata query service; `report_definitions` CRUD; builder UI (object → fields → filters → group/sort → aggregates); dense results table + optional chart + CSV export | **L** (~2–3d) | End-to-end create→save→edit→run→export for leads, activities, opportunities (+ contacts/accounts) |
| **4** | Unify analytics: shared `formatNumber/Currency/Percent/Date` + shared chart + empty/loading/error components; collapse the 3× `_rate`; reconcile + document metric definitions; curated CRM+campaign dashboard surfacing saved reports | **M** (~1.5–2d) | Consistent components + reconciled metrics + dashboard |
| **5** | Interface refinement: codify token layer in `tailwind.config` `theme.extend` (color/space/type/radius/shadow/motion) + `useReducedMotion`; sweep Kanban, record pages, tables, builder, dashboards; all states designed | **M** (~1–1.5d) | CRM + analytics visually consistent with the app |
| **6** | A11y + audit + QA: Kanban keyboard moves, focus rings, AA contrast, ARIA; confirm audit-log coverage of stage + significant mutations; responsive/overflow; QA report | **S–M** (~1d) | A11y-complete, audit-verified, QA report |

**One phase per checkpoint, reviewable diffs.** I present the plan + a
representative sample at each phase boundary before fanning out.

---

## 7. Constraint adherence (how this plan honors the hard rules)

- **Don't break anything:** all migrations additive; the `stage` enum stays and
  is dual-written; reporting/sequencing/inbox untouched; each phase keeps the
  full test suite green.
- **Multitenancy-ready, not multitenant:** nullable `tenant_id` on every new
  table + on `leads`; query service + reporting structured to accept a tenant
  filter; no tenancy enforcement implemented now.
- **Human-in-the-loop:** drag + report-run are user actions (execute normally).
  The `opportunity_stage_changes` row records `source='user'`; agentic
  mutations stay suggest-and-approve via `notifications` + `agent_actions`
  (extended, not bypassed). No new auto-mutation.
- **Safe reporting:** metadata whitelist registry only; parameterized
  SQLAlchemy; **never** user SQL.
- **Additive/reversible migrations:** every new table/column nullable or
  defaulted; down-migrations drop only the new objects.
- **Suggest and approve / reviewable diffs:** stopping here for approval; one
  phase per commit.

### Open questions for you

1. **`@dnd-kit` dependency** — OK to add (`@dnd-kit/core` + `/sortable` +
   `/utilities`)? (Recommended for keyboard a11y in Phase 6.)
2. **Pipelines** — single default pipeline is enough for now (cheapest), with
   the schema supporting multiple later? Or do you want multi-pipeline UI in
   this pass? (Plan assumes **single default pipeline, multi-pipeline-ready
   schema**.)
3. **`owner_id`** — add the nullable column now (design-ready, unused until a
   users/auth layer exists)? (Plan assumes **yes, nullable, no FK**.)
4. **Accounts/Contacts** — introduce the tables in Phase 1 and *link* new
   opportunities, but **leave existing lead handling and the opp contact
   snapshot exactly as-is** (no forced backfill/migration of historical data)?
   (Plan assumes **yes**.)
