# Phase 6 — Job-posting intent: compliant sourcing assessment

> **Status: assessment delivered; Tracks 1–3 chosen and BUILT.**
> The original assessment (below) presented the options + recommendation and
> stopped for a choice. The operator chose Track 1, then Tracks 2 + 3. All three
> are now implemented + tested (Adzuna: `collect_dev_roles`; careers-page:
> `collect_careers`; ATS public boards: `collect_ats` over Greenhouse/Lever/
> Ashby). Paid providers (E) and USAJOBS (D) remain deferred/dropped as noted.

## Why this signal matters

A posted **Development Director / Grant Writer / Foundation Relations / Director
of Philanthropy** role is the single strongest **Tier-1 "act now"** signal for
the GrantMind ICP. It says, unambiguously and with a date: *this org is investing
in grant-seeking right now* — exactly when a grant-writing service should reach
out. It maps to the existing `IntentSignalType.DEV_ROLE_POSTED` (Tier 1, 45-day
half-life) already wired through scoring + the promotion bridge. The only missing
piece is a **compliant collector** to populate it.

## The hard constraint

- **No LinkedIn scraping** — explicit standing constraint for this codebase.
- **No Idealist scraping** — ToS-prohibited. Idealist is the premier nonprofit
  jobs board and has **no public API**, so the single best source for this exact
  ICP is off-limits. This is the core difficulty and the reason the prompt flags
  this phase as "build last."

Everything below respects ToS + robots.txt and uses official APIs or an org's
own published pages.

## Options (live-probed where possible, 2026-06)

| # | Source | Access | Cost | Nonprofit-dev-role coverage | Effort | Risk / caveat |
|---|--------|--------|------|------------------------------|--------|----------------|
| A | **Aggregator job API** (Adzuna, Jooble, Careerjet) | Official REST API, free key (Adzuna 400'd w/o `app_id`+`app_key` — confirmed gated) | Free tier (attribution) → low | **Medium** — broad US coverage incl. some nonprofit roles; partial | **Low** | Recall/precision vary; **employer→monitored-org matching is fuzzy** (name only); free-tier rate caps |
| B | **Org careers-page check** (the org's OWN site) | HTTP fetch + robots.txt respect + LLM extract (reuses existing web_search/scrape) | ~1 LLM call/org/run | **High precision** when the org has a careers page | **Medium** | Per-org cost; pages heterogeneous/JS-rendered; many small orgs have no careers page or no open roles; must honor robots.txt |
| C | **ATS public board APIs** (Greenhouse, Lever, Ashby) | Public JSON, **no auth** — confirmed live: `boards-api.greenhouse.io/v1/boards/wikimedia/jobs` 200, `api.lever.co/v0/postings/charitywater` 200 | Free | **Low–medium** — only orgs on these ATSs (`cityyear` 404'd; `charitywater` worked) | **Low per org**, but needs a **token-discovery step** | Most *small* nonprofits don't use these ATSs; coverage skewed to larger/well-resourced orgs |
| D | **USAJOBS API** (federal) | Official, free key (401'd w/o `Authorization-Key` — confirmed) | Free | **~Zero** — federal jobs only; nonprofits don't post here | Low | Wrong population for this ICP. Note + skip (relevant only if ICP ever includes federal/quasi-federal entities) |
| E | **Paid jobs-data provider** (Coresignal, People Data Labs, Aura, Revelio) | Commercial API/dataset; vendor handles licensing | **$$$** — typically $1k+/mo minimums | **High** — comprehensive, includes nonprofit + (vendor-licensed) LinkedIn-sourced postings | Low–medium integration | Cost; due-diligence that the vendor's sourcing is itself compliant; the "buy your way out" path |
| F | **Nonprofit-specific boards** (Philanthropy News Digest jobs, Work for Good, Chronicle of Philanthropy) | Mostly **no public API**; some RSS (PND) | Free–low | **High precision** for this ICP | Medium–high (partnership / permitted feed only; no scraping) | Best-fit population but least API-accessible; PND RSS is the only clearly-permitted slice |

## Recommendation — two-track, cheap-broad + targeted-precise

The honest headline: **the compliant, accessible sources are imperfect for this
exact ICP** (the best-fit board, Idealist, is off-limits). So rather than chase
one perfect source, combine a cheap broad net with a precise targeted check, and
keep the paid provider as the documented scale path.

1. **Track 1 — broad & cheap (recommended first build): an aggregator API
   (Adzuna).** Query the dev/grant-role titles nationally, filter to nonprofit
   employers, and match each posting to a monitored org by employer name
   (reusing the Phase-2 `matching.normalize_name` self-match logic, extended to
   fuzzy employer matching). Free, compliant, immediate, idempotent
   (`dedupe_key = adzuna:devrole:{posting_id}:{org_id}`). **Accept partial
   recall** — log new/deduped/unmatched counts so the coverage gap is visible,
   per the cross-cutting observability guardrail.

2. **Track 2 — targeted & precise: careers-page check on the org's OWN site,
   run only on already-warm orgs.** When an org is already Tier-2 (e.g. a
   `rev_drop`) or otherwise high-interest, fetch its careers page (robots-
   respecting) and LLM-extract any dev/grant role. This **upgrades a Tier-2 org
   to Tier-1** with high precision at bounded cost (only the high-interest
   subset is fetched, not the whole monitored set). Reuses the existing
   web_search/scrape + Anthropic extraction already in the codebase.

3. **Track 3 — ATS public APIs (Greenhouse/Lever/Ashby) as a free bonus.** For
   the subset of monitored orgs discovered to use these ATSs, poll the public
   board endpoint directly (confirmed free + no-auth). Low effort once a token
   is known; skip orgs not on these ATSs.

4. **Defer the paid provider (E)** until volume/coverage justifies the spend;
   document it as the scale path. **Drop USAJOBS (D)** for this ICP and
   **reaffirm no LinkedIn/Idealist scraping.**

Scoring/promotion need **no changes** — `DEV_ROLE_POSTED` is already a Tier-1
type with its own half-life, ICP signal-weight, and the human-approved draft
bridge. This phase is purely about *sourcing* that signal compliantly.

## What I'd build, per choice

- **Pick Track 1 (Adzuna):** an `adzuna` client + `collect_dev_roles` collector
  (title-filtered search → fuzzy employer→org match → `DEV_ROLE_POSTED` signal)
  + a daily beat task + the free key wired through compose env. ~1 collector,
  mirrors the Grants.gov fan-out pattern. Needs a free Adzuna `app_id`/`app_key`.
- **Pick Track 2 (careers-page):** a `collect_careers_page` enrichment that runs
  on warm orgs, reusing the existing scrape + Anthropic extract, robots-gated.
- **Pick Track 3 (ATS):** an ATS-board client + a per-org token field on `orgs`
  + a collector polling Greenhouse/Lever/Ashby for known-token orgs.
- **Pick E (paid):** scope to a specific vendor after a pricing/compliance review.

Each is idempotent, rate-limit-respecting, logs new/deduped/errored counts, and
feeds the existing scoring → promotion pipeline unchanged.

**Awaiting your choice before building anything.**
