# Tenancy decisions — locked before Phase 1

Recorded 2026-07-05, per the multi-tenant SaaS build brief. Each decision was
proposed with tradeoffs and explicitly approved by the operator. These are the
foundation assumptions for `docs/tenancy-plan.md`; changing any of them after
Phase 2 is a re-plan, not a tweak.

---

## 1. Isolation model — shared DB / shared schema + `tenant_id` + Postgres RLS

**Decision:** One database, one schema. Every tenant-owned table carries a
`tenant_id UUID` column (reusing the exact convention already present on the
crm/intent/lead/report tables). App-level query scoping is the first line of
defense; **Postgres Row-Level Security** keyed on a session GUC
(`app.tenant_id`) is enabled on every tenant-owned table as defense-in-depth.
The runtime connects as a non-owner role so policies actually apply.

**Why:** Matches the partial `tenant_id` groundwork already in the schema (14
tables), keeps a single Alembic history, and is the cheapest to operate for a
solo operator. RLS turns "a missed `WHERE tenant_id`" from a silent
cross-tenant leak into zero rows returned.

**Rejected:**
- *Schema-per-tenant* — stronger isolation, but migrations run N times,
  connection pooling gets complicated, and the existing 42-migration linear
  history doesn't map onto it. Operational cost is disproportionate.
- *App-level scoping only* — one missed filter in any of the 18 routers or
  ~30 worker tasks is a silent P0 leak. The global-sweep workers make this
  too likely.

## 2. Provider keys — full BYOK (all five providers)

**Decision:** Each tenant stores their **own** encrypted credentials for
Anthropic, Brevo, Apollo, Hunter, and Unipile (and Adzuna, used by the intent
engine), encrypted with the existing Fernet contract in
`app/services/encryption.py`. The platform fronts **no** tenant provider
spend. No task or service may fall back to a global env key while in tenant
context. The Settings UI gains enter-key + test-key surfaces per provider.
The only platform-owned provider credential is a **platform Brevo key** used
exclusively for auth/billing transactional email (password resets cannot
depend on a tenant's own key).

**Why:** Zero cost exposure, flat-price plans stay simple (no usage
metering), and — decisive for this product — deliverability and
LinkedIn-automation ban risk stay on each tenant's own Brevo/Unipile
accounts rather than concentrating on platform accounts.

**Rejected:**
- *Platform Anthropic + BYOK rest* — nicer onboarding (AI works instantly)
  but requires per-tenant token metering before every Anthropic call plus
  Stripe metered billing from day one. Deferred: can be added later as a
  premium convenience without unwinding BYOK.
- *Full platform keys* — best UX, worst risk: one Brevo account absorbing
  every tenant's sending behavior is a deliverability disaster in waiting,
  and shared Unipile/LinkedIn exposure is equally bad.

## 3. Auth — first-party email/password, SSO-ready

**Decision:** First-party signup/login/logout/password-reset.
argon2id hashing (`argon2-cffi` directly — passlib is unmaintained).
Server-side sessions: the browser holds a random 256-bit token in an
`httpOnly; SameSite=Lax; Secure` cookie; the DB stores its SHA-256 hash
(revocable, logout-all works). The `User` model carries nullable
`auth_provider` / `external_id` (with a unique pair constraint) so
Google/OAuth SSO can be added later **without a migration** — but no OAuth
code ships in v1.

**Rejected:**
- *OAuth from day one* — external dependency, redirect flows, and test
  complexity in Phase 1 for marginal v1 benefit.
- *JWT / signed-cookie sessions* — not revocable without extra machinery;
  every request hits Postgres anyway, so stateless tokens buy nothing here.

## 4. Plans — 3 tiers, flat quotas, no usage metering

**Decision:** Starter / Pro / Agency at flat monthly prices. Quotas enforced
in-app (API **and** workers, before any provider spend) — Stripe handles
subscription state only, no usage records. Candidate meters (final numbers
proposed at the Phase 5 checkpoint): email sends/month, active leads,
connected sending accounts, LinkedIn accounts, team seats, AI research
runs/month, social-listening searches, active campaigns.

**Why:** Pairs naturally with full BYOK (the tenant pays providers directly,
so the platform doesn't need to meter spend), avoids Stripe metered-billing
plumbing, and keeps entitlement logic a pure function of (plan, counters).

**Rejected:**
- *2 tiers + usage add-ons* — more revenue flexibility, but requires Stripe
  usage records and per-call metering plumbing in Phase 4/5.
- *Single plan + trial* — fastest to ship but no upsell path; quotas would
  still need enforcing to cap abuse, so the savings are small.
