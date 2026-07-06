# Email Blaster — Phase 1.5 roadmap (M2 onwards)

Remaining milestones for the multi-step + LinkedIn work. M1 (sequence
framework, email-only) shipped. Current project state is in
[`/claude.md`](../claude.md).

**Architecture decision underlying M2–M4:** all LinkedIn actions live
behind a `LinkedInProvider` abstract base class. M2 ships a concrete
**DIY implementation wrapping the open-source [`linkedin-api`](https://github.com/tomquirk/linkedin-api)
library** + a residential proxy. The ABC is designed so a hosted
provider (Unipile/HeyReach/etc.) can be added later as a second
concrete implementation when commercial scale demands it — the workers,
schemas, and frontend won't need to change.

### Why DIY for M2 (decision log, 2026-05-12)

- 3-6 months out from external customers — time available to iterate.
- A throwaway LinkedIn account is available for development, so
  ban-during-development risk is contained.
- $0 per seat vs $70/seat means we can ship LinkedIn features without
  raising the price floor on the eventual SaaS tier.
- If/when customer accounts start getting restricted at scale, we layer
  in a hosted provider (likely just for write actions — see M3 hybrid
  note) without rebuilding M2.

---

## M2 — LinkedIn accounts + read-only warm-ups

**Goal:** users connect a LinkedIn account by entering their LinkedIn
credentials; we store an encrypted session; trigger safe warm-up
actions from sequences (profile view, follow, react to a post); and
detect inbound events (connection accepts, DM replies) by polling
LinkedIn directly.

### 0. Library + infrastructure choices (do this before any code)

- **DIY library:** [`linkedin-api`](https://github.com/tomquirk/linkedin-api)
  (PyPI: `linkedin-api`). Add to `backend/requirements.txt`. Covers
  view_profile, follow, message, send_invitation, search,
  get_conversations, get_profile_posts, react_to_post. Built-in
  rate-limit awareness; uses Voyager endpoints under the hood.
- **Proxy provider:** Bright Data residential, or Smartproxy as a
  cheaper alternative (~$15/month for low single-user volume). For
  multi-account deployments later, scale to a per-account proxy pool.
  Configured via env vars `LINKEDIN_PROXY_URL` (e.g.
  `http://user:pass@proxy.brightdata.com:22225`) and per-account
  overrides in `LinkedInAccount.proxy_url`.
- **Optional later:** `LinkedInProvider` ABC + a second concrete impl
  for a hosted provider once the cost/liability trade flips.

Add to `.env`:
```
LINKEDIN_PROXY_URL=http://user:pass@host:port   # optional in dev with throwaway account
LINKEDIN_DAILY_ACTION_CAP=20                    # per-account cap, conservative default
```

### Backend tasks

1. **Provider ABC + DIY concrete impl**
   - `app/services/linkedin/__init__.py`
   - `app/services/linkedin/base.py` — `LinkedInProvider` ABC + shared
     dataclasses (`ProfileRef`, `ActionResult`, `InboundEvent`,
     `ChallengeRequired`).
   - `app/services/linkedin/linkedin_api_impl.py` — concrete impl
     wrapping the `linkedin-api` package. Runs `linkedin_api.Linkedin(...)`
     inside `asyncio.to_thread` since the upstream library is sync.
     Decryption (password + cookies) is **confined to this module** —
     same invariant as `imap_client.py`. A pytest must enforce it.
   - Methods needed in M2: `login(account) -> SessionToken`,
     `test_connection(account)`, `view_profile(account, profile_url)`,
     `follow_profile(account, profile_url)`,
     `react_to_post(account, post_urn, reaction='LIKE')`,
     `latest_post_urn(account, profile_url) -> str | None`,
     `inbox_recent_events(account, since) -> list[InboundEvent]`,
     `check_connection_status(account, profile_urn) -> str`.
2. **LinkedInAccount model + migration**
   - `app/models/linkedin_account.py`. Fields:
     - `id`, `label`, `linkedin_email` (login)
     - `password_encrypted` (Fernet) — needed for re-auth when cookies
       expire
     - `session_cookies_encrypted` (Fernet, populated after first
       successful login; `li_at` + `JSESSIONID` JSON-encoded)
     - `proxy_url` (nullable; falls back to `settings.LINKEDIN_PROXY_URL`)
     - `status` enum: `untested | ok | failed | challenged | restricted`
     - `last_error`, `last_tested_at`, `last_polled_at`, timestamps.
   - `alembic/versions/0003_linkedin_accounts.py` — new table +
     `campaigns.linkedin_account_id` FK (nullable, `ON DELETE SET NULL`).
   - Update `tests/conftest.py` truncate list.
3. **Router**
   - `app/routers/linkedin_accounts.py` — CRUD + `/test` + a
     `/{id}/resolve-challenge` endpoint for the case where LinkedIn
     requires a captcha/PIN/2FA. The test endpoint returns
     `{ status: "challenged", challenge_url: ... }` so the UI can
     surface the challenge.
   - Register in `app/main.py`.
4. **Sequencer integration**
   - Channel handler `send_linkedin_step(lead_id, node_id)` in
     `app/workers/sequencer.py`. Dispatches to the provider per
     `node.kind`. On `ChallengeRequired` exception, mark the
     `LinkedInAccount.status='challenged'` and skip the step (don't
     retry until the user resolves it).
   - Extend the dispatch switch in `_advance_sequences_async` to handle
     `LINKEDIN_VIEW_PROFILE`, `LINKEDIN_FOLLOW_PROFILE`,
     `LINKEDIN_REACT_POST`.
   - When the campaign has no `linkedin_account_id`, write
     `lead_step_executions(result=skipped, error="no LinkedIn account
     configured")` and advance.
5. **Publish gate**
   - In `sequence_service.PUBLISHABLE_KINDS_M1`, add the three read-only
     LinkedIn kinds. Keep write kinds rejected until M3.
6. **Inbox polling (primary path, since we don't have webhooks)**
   - New beat task `linkedin_poll_accounts` calling
     `provider.inbox_recent_events(account, since=last_polled_at)` for
     each `ok` account every 5 min (configurable via
     `LINKEDIN_POLL_INTERVAL_MINUTES`, default 5).
   - Translates events to:
     - `Lead.linkedin_connection_status` updates
       (`invited` → `connected`, etc.) — match by `lead.linkedin_url`.
     - `Lead.linkedin_last_reply_at` when an inbound DM arrives from a
       matched lead.
   - Updates `account.last_polled_at` regardless of event count.
7. **Rate limiting (HARD — bans depend on this)**
   - Per-account daily cap on LinkedIn actions
     (`settings.LINKEDIN_DAILY_ACTION_CAP`, default 20). Reuse the Redis
     counter pattern from `workers/send.py`.
   - Minimum delay between actions per account (default 90 seconds,
     configurable).
   - **Skip-on-challenged:** if `account.status='challenged'` or
     `'restricted'`, all dispatches for that account skip with a
     descriptive error until the user clears it in the UI.
8. **Encryption invariant test**
   - Extend `test_phase15_hardening.py` (or add a sibling) to assert
     that `encryption.decrypt(` only appears in `imap_client.py` AND
     `services/linkedin/linkedin_api_impl.py`. Both modules are
     allowed; everything else is forbidden.

### Frontend tasks

1. **Palette in `SequenceBuilder.jsx`** — add three new entries for the
   M2 kinds.
2. **NodeEditor** — per-kind config UI:
   - `linkedin_view_profile`: title only.
   - `linkedin_follow_profile`: title only.
   - `linkedin_react_post`: `{ reaction: "LIKE" | "CELEBRATE" | "SUPPORT" | "FUNNY" | "LOVE" | "INSIGHTFUL" }`
     dropdown (Voyager enum values).
3. **Campaign wizard / detail** — let the user pick a connected LinkedIn
   account (dropdown like the existing inbox one).
4. **Settings page** — new "LinkedIn accounts" section parallel to
   "Connected inboxes".
   - `frontend/src/components/ConnectLinkedInModal.jsx` — email +
     password fields; warning that for active accounts a residential
     proxy is strongly recommended; optional `proxy_url` override field
     for power users.
   - **Challenge resolution UI:** if the test returns
     `status: "challenged"` with a `challenge_url`, the modal shows the
     URL inline; user opens it in their own browser, completes the
     captcha/PIN, returns to the modal, clicks "I've resolved it" which
     calls `/resolve-challenge`.
   - `frontend/src/api/linkedinAccounts.js`

### Acceptance criteria

- User connects a LinkedIn account through Settings using email +
  password; status flips to "Connected" after the in-modal test, or
  surfaces the challenge URL if LinkedIn demands one.
- A campaign with `linkedin_account_id` set + a sequence
  `[email entry] → wait 1d → view_profile → wait 2d → follow_profile`
  publishes without errors.
- Manually ticking `_advance_sequences_async` for a lead at the
  `view_profile` node dispatches via `linkedin-api`, writes a
  `lead_step_executions(result=sent, external_id=<urn>)`. Confirmed by
  hitting `linkedin-api` directly against the throwaway test account.
- The polling beat task picks up an inbound DM from a known lead and
  sets `Lead.linkedin_last_reply_at`; subsequent edge evaluation with
  `{"op": "replied"}` returns True.
- ≥25 new backend tests; existing 293 stay green.
- Per-account daily cap enforced: 21st action of the day returns
  skipped with a "daily cap reached" error.

---

## M3 — LinkedIn write actions ✅ SHIPPED 2026-05-13

**Goal:** connect requests, DMs, page invites — the actions that
generate replies.

### Backend

- Step kinds (already in the enum, no migration needed):
  `linkedin_connect`, `linkedin_dm`, `linkedin_invite_to_page`.
- `linkedin_dm` MUST check
  `lead.linkedin_connection_status == 'connected'` before firing,
  otherwise record `skipped, error="not connected — DM requires 1st
  degree"`.
- Expand `PUBLISHABLE_KINDS_M1` (rename to `PUBLISHABLE_KINDS`) to allow
  these.
- Per-action config validated at publish:
  - `linkedin_connect.note_template` — ≤ 300 chars.
  - `linkedin_invite_to_page.page_id` — required.
- Provider methods to add:
  `send_connect_request(account, profile_urn, note)`,
  `send_dm(account, profile_urn, text)`,
  `invite_to_page(account, profile_urn, page_id)`.
- **Tighter caps for write actions** (configurable):
  - `LINKEDIN_DAILY_CONNECT_CAP`, default **15/day** per account
    (LinkedIn flags accounts above ~20/day).
  - `LINKEDIN_DAILY_DM_CAP`, default **30/day**.
  - These layer on top of the M2 overall daily cap.

### Hybrid-provider option (decision point for M3)

At the start of M3, reassess whether to add a hosted provider
(Unipile/HeyReach) **specifically for write actions** while keeping the
DIY path for M2 reads. Architecture supports both — `LinkedInProvider`
ABC, one impl per kind family if needed. Trigger to flip:
- Your test account hits a restriction during M2 development, OR
- External customers are imminent (<3 months out).

### Frontend

- Palette + per-kind editors.
- Char counter on the connect-note input.
- "Send test action" button in the per-node editor for write actions
  (fires against a test LinkedIn URL the user supplies).

### Acceptance

- End-to-end manual test: create a campaign, build a 5-step sequence
  including a connect request and a DM, publish, run a real lead
  through it. Confirm both actions appear in the LinkedIn account's
  Sent / Activity log.

---

## M4 — InMail + comment_post + branching UI polish ✅ SHIPPED 2026-05-13

**Goal:** premium actions + a real condition editor that doesn't
require JSON.

### Backend

- `linkedin_inmail` step kind. Requires Sales Nav / Premium on the
  user's LinkedIn account; provider returns an error otherwise. Surface
  that error inline in the campaign detail page.
- `linkedin_comment_post` step kind. Config:
  `{ comment_template, target: "latest" | "most_engaged" }`. Higher
  risk — flag in UI.
- Optional: feature flag system
  (`app/services/feature_flags.py`) for kinds users opt into per
  account.

### Frontend

- **Visual condition builder.** Replace the JSON textarea with a small
  expression-builder UI: AND/OR/NOT grouping, leaf op picker, parameter
  inputs. Persist the same JSON unchanged.
- **Recommended preset sequences** — drop-in templates ("Classic
  3-touch", "Warm intro → connect → DM") the user can apply with one
  click.

### Acceptance

- A new user can build a 6-step branching sequence without ever editing
  JSON by hand.

---

## M5 — Sequence analytics ✅ SHIPPED 2026-05-13

**Goal:** per-step funnel metrics so users see where leads drop off and
which steps drive replies.

### Backend

- **Soft-delete sequence nodes.** Add `sequence_nodes.deleted_at`.
  Update `replace_graph` to soft-delete instead of hard-delete so we
  keep historical `lead_step_executions` for closed nodes. Update the
  scheduler to skip soft-deleted nodes.
- New endpoint `GET /campaigns/{id}/sequence/analytics`:
  - Per node: total entered, completed (advanced past), skipped,
    failed.
  - Per step: open rate / reply rate / connect-accept rate (LinkedIn).
  - Funnel chart data: entered → completed → next node.
- New beat task `compute_sequence_analytics` for caching.

### Frontend

- Replace the read-only sequence view on `CampaignDetail` with a live
  funnel: each node renders with sent / skipped / failed counts; edges
  show the % of leads that took each branch.
- Recharts visualization for the funnel.

### Acceptance

- Looking at a 5-step sequence, a user can identify at a glance which
  step is leaking leads (low advance rate) and which branch dominates.

---

## Cross-cutting tasks

Not milestone-scoped — pick up when convenient.

- **Replace the anonymous `node_modules` Docker volume** with a named
  volume + a `docker compose run` that uses the live volume. Fixes the
  test-environment drift bug noted in `claude.md`.
- **Sequence templates / sharing** — let users export/import sequences
  or share via URL.
- **Multi-tenant readiness** — when commercializing: user accounts,
  org scoping, billing. Out of scope for Phase 1.5.
- **Document the condition language** — `docs/condition_language.md`
  with every op + example. Useful once we start onboarding non-engineer
  users.
