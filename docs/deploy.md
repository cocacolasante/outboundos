# Deploy runbook — multi-tenant SaaS

How to stand up (or upgrade) a hosted deployment.  The multi-tenancy
conversion history and design live in [`tenancy-plan.md`](tenancy-plan.md)
/ [`tenancy-decisions.md`](tenancy-decisions.md); this is the operational
checklist.

## 1. Environment matrix

**Platform secrets (hard-required — the app refuses to boot without):**

| Var | Notes |
|---|---|
| `SECRET_KEY` | long random string; signs unsubscribe links.  NEVER the dev default. |
| `ENCRYPTION_KEY` | Fernet key for stored secrets (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |

**Tenancy / RLS (required for real isolation):**

| Var | Notes |
|---|---|
| `DATABASE_URL` | owner role — Alembic + the narrow service paths |
| `APP_DATABASE_URL` | **non-owner `app_user` role — REQUIRED in production.**  Owners bypass RLS; the boot log prints a CRITICAL warning if you skip this. |

**Platform email (auth mail: password resets, invites):**
`PLATFORM_BREVO_API_KEY`, `PLATFORM_SENDER_EMAIL`, `PLATFORM_SENDER_NAME`.
Distinct from any tenant's Brevo key.

**Stripe (billing):** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`STRIPE_PRICE_STARTER|PRO|AGENCY`, `BILLING_TRIAL_DAYS` (14),
`BILLING_GRACE_DAYS` (7).  Empty = billing disabled (dev).

**Hardening levers:** `API_RATE_LIMIT_PER_MINUTE` (600; 0 disables),
`SUPERADMIN_EMAILS` (comma list — gates `/admin`),
`BILLING_REQUIRE_SUBSCRIPTION` (flip to `true` once every real tenant is
on a plan — denies spend to NULL-status legacy tenants),
`SESSION_COOKIE_SECURE=true` (HTTPS deployments), `ENCRYPTION_KEYS_OLD`
(rotation only).

**Provider keys (`ANTHROPIC_API_KEY` etc.):** BYOK — tenants enter their
own in Settings → Integrations.  Env values only seed the oldest tenant
at migration 0047 and serve tenant-blind fallbacks.  A hosted platform
normally leaves them EMPTY.

## 2. First boot

```bash
docker compose build backend worker beat
docker compose up -d postgres redis
docker compose run --rm backend alembic upgrade head

# Non-owner runtime role (RLS enforcement):
docker compose exec -T postgres psql -U emailblaster -d emailblaster \
  -v app_password="'<STRONG-PASSWORD>'" -f - < backend/scripts/bootstrap_db.sql
# then set APP_DATABASE_URL=postgresql+asyncpg://app_user:<pw>@postgres:5432/emailblaster

docker compose up -d backend worker beat frontend
```

Create the first workspace via the signup UI, or headless:

```bash
docker compose exec backend python scripts/bootstrap_tenant.py \
  --email you@company.com --password '<pw>' --name "Your Workspace"
```

### Verify RLS is actually enforcing

```bash
docker compose logs backend | grep -i "SECURITY"   # must print NOTHING
docker compose exec postgres psql -U emailblaster -d emailblaster -tc \
  "select count(*) from pg_policies where policyname='tenant_isolation'"  # 42
```

If the boot log shows `SECURITY: ... row-level security is BYPASSED`,
the runtime is on the owner DSN — fix `APP_DATABASE_URL` before serving
traffic.  The definitive check is the isolation suite:
`docker compose run --rm backend pytest tests/test_phase63_rls_isolation.py`.

## 2b. Per-tenant Unipile webhooks (Phase 8)

Each tenant's own Unipile workspace needs OUR webhooks registered on it,
or their LinkedIn replies / connection-accepts never reach the app.
Self-service: Settings → Integrations → Unipile → **Register webhooks**
(idempotent — re-run after any `WEBHOOK_BASE_URL` change).  Creates the
three webhooks pointing at `/webhooks/unipile/{tenant_id}` with a
per-tenant secret stored in the tenant's encrypted creds blob.  The
platform-level `/webhooks/unipile` + `UNIPILE_WEBHOOK_SECRET` remain for
a workspace you operate yourself (dev / legacy).

## 3. Stripe webhook

Dashboard → Webhooks → endpoint `https://<host>/webhooks/stripe`, events:
`checkout.session.completed`, `customer.subscription.created`,
`customer.subscription.updated`, `customer.subscription.deleted`,
`invoice.payment_failed`, `invoice.paid`.  Put the signing secret in
`STRIPE_WEBHOOK_SECRET`.  Replay-safe: signatures are verified with
Stripe's timestamp tolerance AND every event id is deduped through the
`webhook_events` ledger.

## 4. Upgrades

```bash
docker compose build backend worker beat        # requirements may change
docker compose run --rm backend alembic upgrade head
docker compose up -d --force-recreate backend worker beat  # restart ≠ re-read .env
```

Migrations always run as the OWNER (`DATABASE_URL`) — never as app_user.

## 5. Key rotation

See the runbook in `backend/app/services/encryption.py`: retire the key
into `ENCRYPTION_KEYS_OLD`, set a fresh `ENCRYPTION_KEY`, force-recreate,
run `python scripts/rotate_encryption.py`, then drop the old key.

## 6. Security checklist (final pass, Phase 7)

- [x] RLS on all 42 tenant-owned tables, fail-closed on missing GUC;
      runtime on NOBYPASSRLS `app_user`; boot tripwire when it isn't.
- [x] Isolation suite (`test_phase63`): DB-level + worker-context +
      router-level (two real signups) — keep it green forever.
- [x] Sessions: httpOnly SameSite=Lax cookies (set `SESSION_COOKIE_SECURE`),
      server-side revocable (SHA-256 hashes), argon2id passwords,
      enumeration-proof login, reset revokes all sessions.
- [x] CSRF origin-check middleware on mutating requests (webhooks exempt).
- [x] Webhook replay: Stripe = signature + timestamp tolerance + event-id
      dedup; Unipile/Brevo = static header + event-id dedup; unsubscribe =
      HMAC token, GET side-effect-free.
- [x] Secrets: Fernet(MultiFernet) at rest; decrypt confined to
      `{imap_client, encryption, tenant_keys}` (grep-enforced); API
      returns masked last-4 only; per-unit-of-work creds cache (never
      module-global).
- [x] Tenant-blind surfaces confined to `get_service_db` /
      `service_worker_engine` allowlists (grep-enforced) with explicit
      tenant stamping on writes.
- [x] Per-tenant API rate limit (fail-open) + per-tenant Redis namespaces
      for domain caps and Brevo watermarks.
- [x] Billing state only ever projected server-side from Stripe webhooks;
      quota checks run pre-spend in API AND workers.
- [x] `/admin` gated by `SUPERADMIN_EMAILS`; every action (impersonation,
      plan override) writes `admin_audit`; impersonation memberships are
      never persisted.
