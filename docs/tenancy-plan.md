# Multi-tenancy conversion plan (Phase 0 audit + design)

Written 2026-07-05. Companion to [`tenancy-decisions.md`](tenancy-decisions.md)
(the four locked decisions: shared-DB + RLS · full BYOK · email/password
SSO-ready auth · 3 flat-quota tiers). Every inventory below was verified
against the code, not guessed: 43 `__tablename__`s, 18 routers (18
`include_router` calls in `app/main.py`), 28 beat-schedule entries in
`app/workers/celery_app.py`, `tenant_id` present in exactly 4 model files,
provider-key reads in 20 modules. Alembic is linear at revision **0042**.

**The P0 risk is cross-tenant data leakage.** Any query, worker sweep, or
cache that can return another tenant's data is a bug of the highest severity.
The design therefore layers three defenses: app-level scoping (explicit
filters + insert stamping), Postgres RLS (fail-closed when the tenant GUC is
unset), and a non-owner runtime DB role (so RLS actually applies).

---

## 1. Table inventory (43 tables)

Classification: **T** = tenant-owned (direct `tenant_id`, RLS-enabled),
**C** = child (gets a denormalized `tenant_id` for RLS; parent FK cascade
preserved), **G** = app-global (no `tenant_id`, no RLS).
"has tid" = nullable `tenant_id UUID` already present today (14 tables).
Backfill: all existing rows go to the single **bootstrap tenant** in the same
migration that adds each column (Phase 2); NOT NULL promotion is a separate,
gated migration.

| Table (model file) | Class | has tid | PK | Conversion notes |
|---|---|---|---|---|
| `campaigns` (campaign.py) | T | — | UUID | **Root of the largest subtree** (leads → events/sequences/…). Composite index `(tenant_id, status)` for the pacer/sweeps. |
| `leads` (lead.py) | T | ✔ | UUID | Parent of ~10 tables. Index `(tenant_id, campaign_id)`, `(tenant_id, lower(email))`. |
| `connected_accounts` (connected_account.py) | T | — | UUID | **App-wide single-default-sender partial-unique index → `UNIQUE (tenant_id) WHERE is_default_sender`.** Default-sender lookups (`routers/connected_accounts.py`, `services/outreach.py:98`) become tenant-scoped. IMAP creds already row-encrypted. |
| `linkedin_accounts` (linkedin_account.py) | T | — | UUID | `unipile_account_id` UNIQUE stays global (one Unipile account can't serve two tenants). Webhook correlation resolves tenant from the row. |
| `agent_settings` (agent.py) | T | — | **int** | **Singleton (`AGENT_SETTINGS_SINGLETON_ID=1`) → per-tenant**: UUID PK + `UNIQUE(tenant_id)`; `agent_core.get_agent_settings()` becomes get-or-create-for-tenant. Absorbs `OWNER_NOTIFY_EMAIL`/`OWNER_NOTIFY_NAME` as per-tenant columns. Existing row migrates to the bootstrap tenant. |
| `notifications` (agent.py) | T | — | UUID | `dedup_key` UNIQUE → `UNIQUE(tenant_id, dedup_key)`. |
| `agent_actions` (agent.py) | T | — | UUID | Append-only audit; tenant_id + index on `(tenant_id, created_at)`. |
| `suppression_list` (suppression.py) | T | — | UUID | **`UNIQUE(email)` → `UNIQUE(tenant_id, email)`** — an unsubscribe is from one sender (compliance-correct); loses cross-tenant "known bad address" sharing (accepted). |
| `research_cache` (research_cache.py) | T | — | **TEXT (email)** | **PK=email → surrogate UUID PK + `UNIQUE(tenant_id, email)`** (incl. the `company::<domain>` namespaced keys). Per-tenant because the tenant's own Anthropic key paid for the blob — sharing would leak one tenant's research to another. |
| `icp_profiles` (icp.py) | T | — | UUID | Single-profile pattern → one per tenant (`UNIQUE(tenant_id)` if kept singular). |
| `lookalike_candidates` (icp.py) | C | — | UUID | `dedup_key` UNIQUE → `UNIQUE(tenant_id, dedup_key)`. |
| `signal_watches` (signals.py) | T | — | UUID | Duplicate-watch 409 logic keys become tenant-scoped. |
| `prospect_signals` (signals.py) | T | — | UUID | `dedup_key` UNIQUE → `UNIQUE(tenant_id, dedup_key)`. |
| `funding_enrichment_queue` (funding.py) | T | — | UUID | `dedup_key` UNIQUE → `UNIQUE(tenant_id, dedup_key)`. Discovery becomes per-tenant (feature-flagged per tenant). |
| `social_listening_searches` (social_listening.py) | T | — | UUID | — |
| `social_listening_posts` (social_listening.py) | C | — | UUID | `UNIQUE(provider, post_url)` → `UNIQUE(tenant_id, provider, post_url)` (two tenants may discover the same public post). |
| `social_listening_opportunities` (social_listening.py) | C | — | UUID | — |
| `sequences`, `sequence_nodes`, `sequence_edges`, `lead_sequence_states`, `lead_step_executions` (sequence.py) | C ×5 | — | UUID | Denormalize tenant_id from campaign/lead. `lead_sequence_states` `UNIQUE(lead_id)` unchanged (lead-scoped). Hot index: `(tenant_id, status, next_run_at)` on states for the sequencer beat. |
| `email_events` (email_event.py) | C | — | UUID | Denormalize from lead/campaign. |
| `style_corrections` (style_correction.py) | C | — | UUID | — |
| `reply_outcomes`, `campaign_copy_insights` (copy_feedback.py) | C ×2 | — | UUID | — |
| `crm_opportunities` (crm.py) | T | ✔ | UUID | Bare `owner_id` UUID gains FK → `users.id` in Phase 1+ (kept SET NULL). |
| `crm_activities` (crm.py) | T | ✔ | UUID | — |
| `crm_documents`, `crm_opportunity_products` (crm.py) | C ×2 | — | UUID | Denormalize from opportunity. |
| `pipelines`, `opportunity_stages` (crm.py) | T/C | ✔ | UUID | "Default pipeline" lookup becomes per-tenant; each new tenant gets the seed pipeline (signup-time seeding, mirroring migration 0038's seed). |
| `accounts`, `contacts` (crm.py) | T ×2 | ✔ | UUID | NB: CRM records, NOT the billing entity — the customer entity is named **`Tenant`** (naming collision guard from the brief). |
| `opportunity_stage_changes` (crm.py) | C | ✔ | UUID | — |
| `report_definitions` (report.py) | T | ✔ | UUID | `report_query.py` already accepts optional tenant_id — becomes mandatory in tenant context. |
| `orgs`, `signals` (intent.py), `org_intent_scores`, `icp_intent_profiles` | T ×4 | ✔ | UUID / org_id-FK | `uq_orgs_tenant_ein` (`nulls_not_distinct`) is the **template constraint** for the rest. `signals.dedupe_key` UNIQUE → `UNIQUE(tenant_id, dedupe_key)`. |
| `webhook_events` (webhook_event.py) | **G** | — | UUID | Dedup ledger for Unipile/Brevo **and the future Stripe webhook**. Stays global. |
| `linkedin_profile_cache` (linkedin_profile_cache.py) | **G** | — | TEXT (slug) | Public slug→URN map, no customer data. Stays global. |
| `funding_source_state` (funding.py) | **G** | — | TEXT (source) | External-feed cursor stays global; the per-tenant *config* currently in its `config` JSONB (states, lookback, enabled) moves to per-tenant settings (Phase 4). |

New tables (later phases): `tenants`, `users`, `memberships`,
`user_sessions`, `auth_tokens` (Phase 1, no RLS — identity layer);
`tenant_provider_keys` (Phase 4, RLS); `usage_counters` (Phase 5, RLS).
Every new table must be added to the hand-maintained TRUNCATE list in
`backend/tests/conftest.py` (currently complete at 43/43).

## 2. Router inventory (18 routers, `app/main.py:114-131`)

**No authentication exists anywhere today** — the only dependency is
`Depends(get_db)` (`app/database.py:27`). Conversion rule: `get_db` itself is
redefined to resolve user → active tenant → tenant-scoped session (§6), so
router *signatures* don't change; per-router work is fixing the
singleton/global lookups listed below, plus explicit `tenant_id` filters on
hot paths (RLS is the backstop, not the plan).

| Router | Prefix | Tenant-scoping work beyond the shared dependency |
|---|---|---|
| `campaigns.py` | `/campaigns` | Largest surface (~28 endpoints). `list_campaigns` unscoped select; all lead sub-resources trust `campaign_id` path param — RLS covers, add explicit filters on list endpoints. |
| `leads.py` | — | Global `select(Lead)` list + filters; already has tenant_id column. |
| `crm.py` | `/crm` | `GET /pipelines/default` + pipeline summary become per-tenant; per-tenant pipeline seeding at signup. |
| `agent.py` | `/agent` | **Singleton**: `agent_core.get_agent_settings()` id=1 → per-tenant get-or-create. Notifications/actions lists scoped. |
| `connected_accounts.py` | `/connected-accounts` | Default-sender promote/demote becomes tenant-scoped (partial-unique index change, §1). |
| `linkedin_accounts.py` | `/linkedin-accounts` | List/detail scoped; hosted-auth callback correlates by row UUID (unchanged). |
| `icp.py` | `/icp` | Single `/profile` row → per-tenant profile. |
| `intent_profiles.py` | `/intent` | Single-active `/activate` flips others *within tenant*. **Admin-ish ops (`/orgs/seed`, `/collectors/run`, `/recompute`, `/promote`) get auth + role gating** (admin/owner), later super-admin. |
| `signals.py` | `/signals` | Watch/signal lists scoped; funding-source toggles become per-tenant config (Phase 4); run-now gated by role. |
| `social_radar.py` | `/social-radar` | Search/opportunity lists scoped. |
| `sequences.py`, `preview.py`, `analytics.py` | — | Campaign-path-scoped; RLS covers, spot-add filters. |
| `reports.py` | `/crm/reports` | Aggregations gain tenant filter (they're full-table rollups today). |
| `report_builder.py` | `/reports` | Already tenancy-ready: thread `current_tenant_id` into `report_query.run_report` (its optional `tenant_id` becomes mandatory). |
| `research_client.py` | `/research-client` | Default-sender lookup scoped; BYOK Anthropic/Brevo creds resolved per tenant. |
| `settings.py` | `/settings` | `/api-status` reports the *tenant's* key status (BYOK); `/brevo/sync-blocklist` becomes per-tenant + role-gated. |
| `webhooks.py` | — (root) | **Tenant-blind by design** — service-engine path (§6): unsubscribe resolves lead by HMAC-signed id then enters that row's tenant context; Unipile events resolve tenant via `linkedin_accounts`/lead rows. |
| *(new)* `auth.py`, `billing.py`, `stripe_webhooks.py` | `/auth`, `/billing` | Phase 1 / Phase 5. |

Frontend: single axios client (`frontend/src/api/client.js`) — add
`withCredentials: true` + a 401→login interceptor (one place, all 17 API
modules inherit). Flat routes in `App.jsx` get an auth wrapper; thin login
page ships in Phase 1 so the app stays drivable.

## 3. Worker inventory (28 beat entries + enqueued tasks)

Uniform pattern today: sync task wrapper → `asyncio.run(_async())` → fresh
`create_async_engine(settings.DATABASE_URL)` per invocation. Conversion:
engines switch to `APP_DATABASE_URL` (non-owner role, §6); each unit of work
runs inside `run_for_tenant(tenant_id)` which sets the ContextVar the GUC
listener reads. Two strategies:

- **derive-from-record** — single-record tasks keep their signatures; a
  narrow service-engine helper `tenant_of(Model, pk)` reads the row's
  `tenant_id`, then the task enters tenant context. No broker message-format
  migration; in-flight tasks survive the deploy.
- **dispatcher-fanout** — each global-sweep beat task becomes a dispatcher
  that lists active tenants (service engine) and enqueues a per-tenant
  subtask with an explicit `tenant_id` arg. Sweep bodies are unchanged (they
  just run under RLS); per-tenant failure isolation (one tenant's revoked
  key can't abort everyone); each subtask stays under the 300s
  `visibility_timeout`.

| Beat task | Today | Strategy |
|---|---|---|
| `sequencer.advance_sequences` (60s) | global select over due `lead_sequence_states` joined to RUNNING campaigns | dispatcher-fanout per tenant |
| `send.pace_first_emails` (60s) | `select(Campaign).where(status==RUNNING)` (`send.py:600`) | dispatcher-fanout |
| `reply_poller.poll_all_replies` (20m) | all `connected_accounts` | dispatcher-fanout (tenant from account row) |
| `linkedin_poller.poll_all` (30m) | all OK `linkedin_accounts` | dispatcher-fanout |
| `lead_sweeper.sweep_stale` (5m) | global stale RUNNING leads | dispatcher-fanout |
| `social_listening.scheduled_runner` (60s) | due searches | dispatcher-fanout (or keep dispatcher global + derive tenant per search in `run_search`) |
| `signals.scheduled_runner` (60s) | due watches | same as above |
| `agent_sweeper.sweep_reminders` (30m) / `sweep_stale_opps` (1h) | global CRM sweeps | dispatcher-fanout |
| `copy_insights.refresh_all` (1h) | group-by campaign | dispatcher-fanout |
| `deliverability.sweep_health` (15m) | RUNNING campaigns | dispatcher-fanout |
| `funding.retry_enrichment` (daily) | due queue rows | dispatcher-fanout |
| `brevo_events_poller.poll` (10m) | **single global BREVO key + single Redis watermark** | per-tenant: tenant's BYOK Brevo key + watermark `brevo:events:watermark:{tenant_id}`; skip tenants without a Brevo key |
| `brevo_blocklist_sync.sync` (6h) | single key → global suppression | per-tenant key → per-tenant suppression |
| `digest.send_daily` | one `OWNER_NOTIFY_EMAIL` | per-tenant digest to the tenant's notify email (from per-tenant AgentSettings); dedup key `digest:{tenant_id}:{date}` |
| `icp.refresh_profile` / `icp.discover` (daily) | single profile | per-tenant fanout, skip tenants without Apollo/Anthropic keys |
| `funding.poll_usaspending` / `poll_irs_bmf` | env-flagged, global cursor | **split fetch vs stage**: external-feed fetch can stay shared/global-cursor; *staging* (leads/signals/notifications) fans out per tenant with the feature enabled. Per-tenant config moves out of `funding_source_state.config`. |
| `intent.backfill_orgs` + 6 `intent.collect_*` + `recompute_intent` + `promote_eligible` | app-global | same fetch-vs-stage split; org universe + scoring are tenant-owned (tables already have tenant_id) |

Single-record enqueued tasks — all **derive-from-record**:
`ingest.run_campaign_research(campaign_id)`, `research.research_lead(lead_id)`,
`compose.compose_lead(lead_id)`, `send.send_lead(lead_id)`,
`sequencer.send_email_step` / `send_linkedin_step(lead_id, node_id)`,
`social_listening.run_search/expand_topic/qualify_*(search_id/post_ids)`,
`signals.run_watch(watch_id)`, `intent.enrich_draft_contact(lead_id)`.
Each resolves tenant → enters `run_for_tenant` → resolves BYOK creds →
**checks entitlements** → only then spends.

Redis keys: `rate:{campaign_id}:*` and `li-rate:{account_id}:*` are already
tenant-safe (id-scoped). **Collisions to fix in Phase 4:**
`rate:domain:{domain}:*` → `rate:domain:{tenant_id}:{domain}:*`, and the
Brevo events watermark (above).

## 4. Provider keys → BYOK map

All keys are global env today, read through the `@lru_cache`'d `settings`
singleton — the per-tenant model must never route through it for tenant
spend. Verified consumer modules (grep, 2026-07-05):

| Provider | Env keys today | Consumers to convert |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` (+ 7 model-name settings, which stay env) | `services/_anthropic.py` (**module-global cached client — killed**, becomes `get_client(api_key)` factory), inline copies in `services/web_research.py` + `services/research_client.py` (fold into the factory), `social_listening_{discovery,qualifier,topic_expander}.py`, `workers/compose.py`, `routers/{research_client,settings}.py` |
| Brevo | `BREVO_API_KEY`, `BREVO_SENDER_EMAIL/NAME` | `services/brevo.py` (fresh httpx client per call — functions gain `creds: BrevoCreds`), `services/outreach.py`, `workers/brevo_blocklist_sync.py`, `workers/`(events poller), `routers/{research_client,settings}.py` |
| Apollo | `APOLLO_API_KEY` | `services/apollo.py` (+ `workers/research.py`), `routers/settings.py` |
| Hunter | `HUNTER_API_KEY` (+ `HUNTER_DOMAIN_SEARCH_LIMIT`) | `services/hunter.py`, `routers/signals.py`, `routers/settings.py` |
| Unipile | `UNIPILE_DSN`, `UNIPILE_API_KEY`, webhook secret/header | `services/linkedin/unipile_impl.py` — **constructor already accepts injected `dsn`/`api_key`** (clean seam); `routers/linkedin_accounts.py` |
| Adzuna | `ADZUNA_APP_ID/KEY` | `services/funding_sources/adzuna.py`, `routers/intent_profiles.py` |
| IMAP | (none — creds already per-`ConnectedAccount` row, Fernet-encrypted) | no change; the existing pattern is the BYOK template |

**Storage:** `tenant_provider_keys` (RLS-enabled): `tenant_id`, `provider`
enum, `encrypted_credentials` = Fernet token over a **JSON blob** (handles
multi-field creds: unipile `{dsn, api_key}`, brevo
`{api_key, sender_email, sender_name}`), `last_test_status/at/error`,
`UNIQUE(tenant_id, provider)`.

**Resolver:** `services/tenant_keys.py` — typed frozen dataclasses per
provider; `await get_provider_creds(session, provider)` raises
`ProviderNotConfigured` when absent (**no global fallback in tenant
context**); cache per unit-of-work only via `session.info` (never
module-global); decrypt built on `MultiFernet` for key-rotation headroom.
Must be added to the decrypt-allowlist hardening test
(`test_phase15_hardening.py`: `{imap_client.py, encryption.py}` →
`+ tenant_keys.py`); plaintext stays local-scope per the existing contract.

**Threading:** explicit parameters (`creds=` / `api_key=`), **not** a
contextvar — secrets shouldn't be ambient state; params are grep-able and
mockable. The one ambient budget is spent on `tenant_id`.

**Platform env keys after conversion** (`validate_required_settings`):
`DATABASE_URL`, `APP_DATABASE_URL`, `REDIS_URL`, `ENCRYPTION_KEY`,
`SECRET_KEY`, `PLATFORM_BREVO_API_KEY`, `PLATFORM_SENDER_EMAIL`,
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*`. Provider
keys drop out of `_HARD_REQUIRED`. Phase-4 exit gate: grep shows **zero**
remaining `settings.(ANTHROPIC|BREVO|APOLLO|HUNTER|UNIPILE|ADZUNA)_` reads
on tenant paths. (Cleanup while there: `BREVO_WEBHOOK_SECRET` is declared
twice in `config.py`.)

Settings API: `GET/PUT /settings/integrations/{provider}` (store encrypted,
return masked last-4 + test status) + `POST .../test` (one cheap
authenticated call, persists `last_test_status`).

## 5. Identity & auth design

Models in `app/models/identity.py` — **no RLS** (the identity layer is
queried before tenant context exists); UUID PKs like the rest of the schema.

- **`Tenant`** — the customer entity. Named `Tenant` (never `Org`/`Account`/
  `Contact` — those names are taken by prospect/CRM records). Columns: name,
  `slug` UNIQUE, status enum(active|suspended|canceled), billing fields (§7),
  created_at.
- **`User`** — `email` lowercased UNIQUE, `password_hash` nullable,
  `auth_provider` + `external_id` nullable with `UNIQUE(auth_provider,
  external_id)` (SSO-ready), `email_verified_at`.
- **`Membership`** — `tenant_id` + `user_id`, role enum(owner|admin|member),
  `UNIQUE(tenant_id, user_id)`.
- **`UserSession`** — `token_hash` UNIQUE (SHA-256 of the 256-bit random
  cookie token), user_id, tenant_id (active tenant), expires_at,
  last_seen_at, revoked_at, ip, user_agent.
- **`AuthToken`** — purpose enum(password_reset|email_verify), token_hash,
  30-min expires_at, used_at.

Flows: signup = Tenant + owner User + Membership (+ per-tenant seeds:
default pipeline, AgentSettings row) in one transaction. Login sets the
cookie (`httpOnly; SameSite=Lax; Secure` in prod); an Origin-check middleware
adds CSRF belt-and-braces. Password reset: `POST /auth/forgot` always 200;
reset revokes all the user's sessions. Auth email goes through
`services/platform_email.py` (platform Brevo key — a tenant's BYOK key can't
send their own password reset). Hashing: argon2id via `argon2-cffi` in
`app/auth/passwords.py`. Deployment note: session cookies require the app
and API on the same registrable domain (`app.x.com` + `api.x.com` is fine).

## 6. RLS + tenant-context design

**GUC:** transaction-local `SELECT set_config('app.tenant_id', :tid, true)`,
stamped by an `after_begin` listener on the (sync) `Session` class in a new
`app/tenancy/context.py`, reading a `ContextVar[UUID | None]`. Why a
listener, not a set-once contextmanager: `AsyncSessionLocal` autobegins, and
any mid-flight `commit()` starts a fresh transaction — a one-shot SET would
silently vanish and RLS would hide all rows. Why not session-level `SET`:
SQLAlchemy's pool reset is rollback-only, so a session-level GUC leaks
across checkouts of the shared API engine.

**Roles / two DSNs:** the app currently connects as the table owner, which
**bypasses RLS silently**. Runtime moves to `APP_DATABASE_URL` (role
`app_user`: LOGIN, NOSUPERUSER, NOBYPASSRLS, non-owner) for the API engine
in `app/database.py` and every worker engine. The owner `DATABASE_URL`
remains for Alembic and a narrow **service engine**. Role bootstrap via an
idempotent `scripts/bootstrap_db.sql` run from the compose entrypoint (not
an Alembic migration — no role passwords in migration history): CREATE ROLE
+ GRANTs + `ALTER DEFAULT PRIVILEGES`. Plain `ENABLE ROW LEVEL SECURITY`
(not FORCE) so owner-role backfills and the service path see all rows; a
hardening test asserts the runtime role isn't owner and lacks BYPASSRLS.

**Policy (per tenant-owned table):**

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <t>
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
```

Missing GUC → NULL predicate → **zero rows, fail-closed, no error**.
`WITH CHECK` blocks cross-tenant INSERT/UPDATE even if app code forgets to
stamp. Global tables and identity tables get no policies.

**Request path:** session cookie → `get_current_user` → `get_current_tenant`
(active tenant from the session row) → **`get_db` redefined in
`app/database.py`** to depend on the tenant, set the ContextVar, yield the
session. Zero churn in the 18 routers; the conftest
`app.dependency_overrides[get_db]` severs the whole auth subtree, keeping
the ~80 anonymous test files green.

**Insert stamping:** `TenantMixin` with
`tenant_id = mapped_column(ForeignKey("tenants.id"), index=True,
default=lambda: current_tenant_id.get())` — `db.add()` call sites unchanged;
RLS `WITH CHECK` backstops.

**Tenant-blind paths** (unsubscribe, Unipile/Brevo/Stripe webhooks, worker
`tenant_of()` lookups): a dedicated `service_engine` + `get_service_db`
whose sessions set `session.info["rls_exempt"] = True` (the listener skips
them). Pattern: resolve the row globally → read its `tenant_id` → enter
tenant context for downstream work. Guarded by a grep hardening test
(allowlist: `webhooks.py`, `stripe_webhooks.py`, `database.py`,
`tenancy/context.py`) mirroring the `encryption.decrypt(` test.

**Workers:** `run_for_tenant(tenant_id, engine)` async contextmanager sets
the ContextVar around the session; the same listener does the GUC work.
Fresh-engine-per-`asyncio.run` stays (loop-correct), pointed at
`APP_DATABASE_URL`.

## 7. Stripe + entitlements design

- **Billing fields live on `tenants`** (exactly one subscription per tenant,
  3 flat tiers, Stripe is the system of record for history):
  `stripe_customer_id` UNIQUE nullable, `stripe_subscription_id`, `plan`
  enum(starter|pro|agency), `subscription_status`
  enum(trialing|active|past_due|canceled|incomplete), `current_period_end`,
  `trial_ends_at`.
- **Plans in code:** `app/billing/plans.py` `PLANS` dict (quotas per meter);
  Stripe price ids from env (`STRIPE_PRICE_STARTER/PRO/AGENCY`). Code, not
  DB: quotas version with enforcement logic; no runtime editing needed at 3
  tiers.
- **Entitlements:** `app/billing/entitlements.py` —
  `check_quota(session, meter, amount=1)` raising `QuotaExceeded`, and
  `get_entitlements(session)` for the UI. Monthly meters counted in
  `usage_counters(tenant_id, period_yyyymm, meter, count)` with atomic
  upsert-increment (no `count(*)` scans per send). Enforcement **before any
  provider spend** in both API (campaign launch, CSV import, sequence
  publish) and workers (before the Brevo POST in `send.py`, before the
  Anthropic call in research/compose, before the Unipile action in the
  sequencer). Worker denial = visible failed/skipped reason on the
  lead/step, no retry storm. Status gating centralized in one
  `spend_allowed(tenant)` helper: trialing/active → full plan quotas
  (14-day trial); past_due → login + read OK, spend denied after a 7-day
  grace past `current_period_end`; canceled/incomplete/none → spend denied,
  UI banner + checkout CTA. Entitlements always derive from server-side
  Stripe state — never client claims.
- **Webhook:** `routers/stripe_webhooks.py` — `stripe.Webhook.
  construct_event` signature verification, then the existing
  `webhook_events` insert-claim-then-handle idempotency pattern
  (`provider="stripe"`, `event_id=event["id"]`; the pattern lives in
  `routers/webhooks.py`). Runs on `get_service_db`; resolves tenant by
  `stripe_customer_id`. Events: `checkout.session.completed`,
  `customer.subscription.created/updated/deleted`,
  `invoice.payment_failed/paid` — each idempotently projects Stripe state
  onto the tenant columns.
- **Checkout/portal:** `POST /billing/checkout {plan}` (Checkout Session,
  `client_reference_id=tenant_id`) + `POST /billing/portal`.

## 8. Phase roadmap (refined)

Per the brief: one phase at a time, tests green, migrations reversible, dev
instance keeps running on the **bootstrap tenant**, STOP at each checkpoint.

- **Phase 1 — Identity & tenancy core.** Identity models + migration; auth
  router (signup/login/logout/reset); session cookie; `get_current_user` /
  `get_current_tenant`; redefined `get_db` (behind a compat flag so feature
  routers stay open until Phase 3 if preferred — decide at checkpoint);
  platform_email service; conftest bootstrap-tenant fixtures.
  **Refinement: a thin login page + axios `withCredentials` + 401
  interceptor ship here** so the app stays drivable through Phases 2–5.
- **Phase 2 — Data-model tenancy.** Nullable `tenant_id` on every T/C table
  **with backfill to the bootstrap tenant in the same migration** (also
  backfills the 14 pre-existing NULL columns); composite hot-path indexes;
  singleton conversions (AgentSettings, default-sender index, suppression
  unique, research_cache PK); per-tenant seed helpers.
  **Refinement: role bootstrap script + `APP_DATABASE_URL` land here.**
  STOP → then the separate, gated **NOT NULL promotion** migration (it
  counts NULLs and aborts if nonzero; down-path restores nullability). STOP.
- **Phase 3 — Enforcement.** RLS policies on every tenant-owned table
  (**only after NOT NULL** — a NULL-tenant row under RLS is invisible to
  everyone = silent data loss); `TenantMixin` adoption; fix all
  singleton/`.first()` lookups; **cross-tenant isolation suite**:
  `tests/test_rls.py` builds its schema via `alembic upgrade head` + role
  bootstrap, connects as `app_user`, seeds two tenants, asserts (a) tenant A
  cannot read/update/delete tenant B through any router, (b) missing GUC →
  zero rows, (c) `WITH CHECK` rejects cross-tenant writes. Functional tests
  stay on the fast owner-role/no-RLS path. Suite stays green forever after.
- **Phase 4 — Workers & BYOK.** `run_for_tenant` everywhere;
  derive-from-record for single-record tasks; dispatcher-fanout for sweeps;
  per-tenant Brevo watermarks + domain-cap Redis prefixes;
  `tenant_provider_keys` + resolver + settings/test-key API; consumer
  threading per §4; entitlement stub (permissive no-subscription default)
  called before every spend. Exit gates: zero global provider-key reads on
  tenant paths; worker-context isolation tests.
- **Phase 5 — Stripe.** §7 in full; quota numbers proposed at checkpoint;
  `check_quota` flips from permissive stub to enforcing.
- **Phase 6 — Frontend.** Full auth screens, workspace/team management,
  billing UI (plan, checkout, portal, usage meters, past-due/canceled
  states), BYOK settings UI with test buttons, quota-block UX.
- **Phase 7 — Hardening & ship.** Isolation suite expansion; per-tenant API
  rate limiting; seed script + super-admin (impersonation with audit, plan
  overrides); key-rotation path (MultiFernet); deploy runbook; final
  security pass (leakage, secret exposure, webhook replay).

**Compatibility risks handled:** in-flight Celery messages across the
Phase-4 deploy (single-record signatures stable; dispatchers are new task
names so stale beat entries drain harmlessly); the `after_begin` listener is
global on `Session` (the `rls_exempt` session-info guard keeps service
sessions clean); test DB is built by `create_all` as owner (that's why the
RLS suite gets its own alembic-built, `app_user`-connected track + CI role
bootstrap).

## 9. Open questions (flag at checkpoints)

1. **Per-tenant suppression** loses cross-tenant known-bad-address sharing —
   accepted for compliance correctness; revisit a global *advisory* bounce
   list later.
2. **Domain send caps per-tenant vs shared**: default per-tenant
   (`rate:domain:{tenant_id}:{domain}`); if two tenants ever send from the
   same domain, shared caps would be deliverability-safer — note only.
3. **Unipile webhooks are workspace-level** (one DSN per Unipile account):
   under BYOK each tenant brings their own Unipile workspace, so per-tenant
   webhook secrets/URLs need a registration flow in the BYOK settings surface
   (Phase 4 detail).
4. **Should Phase 1 gate feature routers immediately** (all 18 behind auth
   the moment `get_db` is redefined) or keep an env-flag escape hatch until
   Phase 3? Recommend immediate (tests already override; the thin login page
   ships in Phase 1) — decide at the Phase 1 checkpoint.
5. **Model-name + cost-lever settings** (`ANTHROPIC_MODEL`,
   `RESEARCH_WEB_SEARCH_MAX_USES`, LinkedIn caps, …) stay platform env in
   v1; candidates for per-tenant/plan overrides later.
