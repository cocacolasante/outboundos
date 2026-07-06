# Standardized metric definitions

_Phase 4 of the CRM extension consolidated reporting onto one foundation.
This is the single source of truth for how each metric is computed and
displayed, so the same number means the same thing on every surface
(campaign analytics, CRM reports, the dashboard, the report builder)._

## Shared helpers

- **Backend ratio** — `app/services/metrics.py::rate(numer, denom, ndigits=4)`.
  Returns `numer/denom` rounded to 4 decimals, or **`None` when `denom` is
  missing or `≤ 0`** ("no basis to compute" — e.g. 0 sent ⇒ no open rate).
  Previously copy-pasted as `_rate` into three routers; they all import this
  now (`analytics`, `campaigns`, `reports`) so the definition can't drift.
- **Frontend formatting** — `src/utils/format.js`:
  `formatNumber`, `formatCurrency` (USD, no cents), `formatPercent`
  (a 0–1 fraction → `"51.2%"`), `formatDate`, `formatDateTime`.
  **Null/undefined renders as the em-dash `—`** everywhere (previously
  campaign analytics used `--`; now standardized).

## Rate metrics (0–1 fraction; `—` when the denominator is 0)

| Metric | Definition | Denominator |
|---|---|---|
| **Open rate** | distinct leads with an `opened` event ÷ sent | `sent_count` |
| **Click rate** | distinct leads with a `clicked` event ÷ sent | `sent_count` |
| **Reply rate** | distinct leads with a `replied` event ÷ sent | `sent_count` (only when reply tracking is configured; else `—`) |
| **Bounce rate** | (hard + soft bounces) ÷ sent | `sent_count` |
| **Delivery rate** | delivered ÷ sent | `sent_count` |
| **Win rate** | won deals ÷ (won + lost) | closed deals in window |
| **Lead→opp rate** | opportunities created ÷ new leads (period) | `new_leads` |
| **Opp→won rate** | won ÷ opportunities created (period) | `opportunities_created` |

> Email-engagement rates count **distinct leads** per event type (a lead that
> opens 5×counts once), matching how open/click/reply rates are normally
> reported.

## Value / pipeline metrics

| Metric | Definition |
|---|---|
| **Won value** | Σ `amount` of deals whose stage is won, `closed_at` in window |
| **Open pipeline value** | Σ `amount` of open (non-closed) deals — a **current snapshot**, not date-scoped |
| **Weighted pipeline** | Σ `amount × probability` over open deals, where probability is the explicit override else the stage's `default_probability` |
| **Avg deal size** | won_value ÷ won_count |
| **Avg sales cycle (days)** | mean(`closed_at − created_at`) over won deals |

## Date-scope conventions

- **Won/lost** figures are scoped by `closed_at ∈ [start, end]`.
- **Open pipeline / weighted / forecast** are a **live snapshot** (where things
  stand now), NOT a historical slice.
- **Leads / conversions / activities** are scoped by their own timestamps.
- The report builder's **relative ranges** (`today`, `yesterday`,
  `last_7_days`, `last_30_days`, `last_90_days`, `this_month`, `this_year`,
  `year_to_date`) resolve server-side in `report_query._resolve_relative_range`.

## Where each surface gets its numbers

- **Campaign analytics** (`/campaigns/{id}/analytics`) — per-campaign email
  engagement, uses `metrics.rate`.
- **CRM reports dashboard** (`/crm/reports/overview`) — pipeline KPIs, win
  rate, forecast, funnel; uses `metrics.rate` + the weighting rule above.
- **Dashboard** (`/dashboard`) — composes the CRM overview KPIs + a
  cross-campaign aggregate (Σ opened ÷ Σ sent, Σ replied ÷ Σ sent) + saved
  reports. Shared `MetricsGrid`, `charts`, and `format` utilities.
- **Report builder** (`/reports/*`) — user-defined aggregates (count / sum /
  avg / min / max) over whitelisted fields; numbers formatted via
  `formatNumber`.
