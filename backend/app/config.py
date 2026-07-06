from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://emailblaster:emailblaster@localhost:5432/emailblaster"

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    # Compose model — writes the actual email copy, so quality matters.
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
    # Research model — just extracts structured facts from web-search results,
    # which a cheaper model handles fine.  Research ingests large web-search
    # result pages as input tokens (the dominant AI cost), so running it on
    # Haiku instead of Sonnet is ~3.75x cheaper on that token spend.
    ANTHROPIC_RESEARCH_MODEL: str = "claude-haiku-4-5-20251001"
    # Web searches allowed per lead's research call.  Each search costs a tool
    # fee AND ingests result pages as input tokens (the dominant per-lead cost),
    # so this is the single biggest cost lever.  Default 1: the merged
    # person+company prompt makes one focused search, which surfaces the top
    # signal for the large majority of leads.  Bump to 2-3 to trade ~+50-100%
    # research spend for marginal extra signal on hard-to-find leads.
    RESEARCH_WEB_SEARCH_MAX_USES: int = 1
    # Freshness window for the cross-campaign research cache (keyed by email
    # or by ``linkedin:<slug>`` for the one-off research-a-client tool).
    # A second campaign adding the same email — or a second click on the
    # same LinkedIn URL — reuses the cached research_data if it was
    # refreshed within this many days, skipping the API call.
    RESEARCH_CACHE_TTL_DAYS: int = 90

    # Research-a-client (one-off LinkedIn URL → outreach) — same extraction
    # task as the bulk pipeline, so we run it on Haiku too.  Was previously
    # ``settings.ANTHROPIC_MODEL`` (Sonnet) which made every "Research a
    # client" click ~4x more expensive than necessary.  Deep mode still
    # gets a larger search budget than fast mode but uses the same Haiku
    # model — the depth is about WHERE we search, not which model writes.
    ANTHROPIC_RESEARCH_CLIENT_MODEL: str = "claude-haiku-4-5-20251001"
    # Per-call web_search budget for the one-off tool.  Fast mode 2 covers
    # identity + a recent news item; deep mode 5 gives Claude room to find
    # podcast / GitHub / Substack signal that fast skips.  Down from 3/8
    # (cost-driven; 8 was rarely surfacing anything the 5th call didn't).
    RESEARCH_CLIENT_FAST_WEB_SEARCH_MAX_USES: int = 2
    RESEARCH_CLIENT_DEEP_WEB_SEARCH_MAX_USES: int = 5

    # Social Listening Radar — discovery is "find LinkedIn posts matching
    # this query, extract structured fields from web-search results."  Same
    # extraction-shape task as research_person_web, so default to Haiku.
    # A single bad-luck run on Sonnet was costing $5+ because each of
    # max_queries_per_run (20) Anthropic calls was ingesting ~90K tokens
    # of web-search-result pages on Sonnet input pricing.  Haiku is ~3.75x
    # cheaper on the same input + output, bringing a full run to ~$1.50.
    ANTHROPIC_SOCIAL_DISCOVERY_MODEL: str = "claude-haiku-4-5-20251001"
    # Web searches allowed per discovery call.  Each search ingests result
    # pages as input tokens AND has a per-search tool fee, so this is a
    # direct cost lever.  2 is enough to find recent posts for a single
    # query; 3 was the original default and rarely surfaced anything new
    # the second pass didn't.
    # Per non-LinkedIn discovery call.  Set to 3 (up from 2) so the
    # discovery prompt can actually try 2-3 keyword variations of each
    # query before bailing — the failure mode was Anthropic running
    # exactly one literal search per query and returning 0 results.
    SOCIAL_DISCOVERY_WEB_SEARCH_MAX_USES: int = 3
    # LinkedIn-specific overrides.  LinkedIn aggressively blocks crawlers
    # (Google indexes very little of it), so the default budget gets
    # almost nothing back.  More uses + Sonnet (better at finding the
    # buried indexed-but-rare content) help recall a lot.  Per-run cost
    # impact when LinkedIn is enabled: ~+$0.50.
    LINKEDIN_DISCOVERY_MODEL: str = "claude-sonnet-4-6"
    LINKEDIN_DISCOVERY_WEB_SEARCH_MAX_USES: int = 5

    # Email sending
    BREVO_API_KEY: str = ""
    BREVO_SENDER_EMAIL: str = "noreply@example.com"
    BREVO_SENDER_NAME: str = "Email Blaster"
    # Set to False to mirror "Click tracking" being turned OFF in the Brevo
    # dashboard (Transactional → Settings).  Disabling click tracking removes
    # Brevo's link-rewriting (a strong bulk/marketing fingerprint that gets
    # 1:1 outreach tagged [BULK]) but also stops CLICKED events from arriving —
    # so when this is False the app reports click-rate as "not tracked" (—)
    # instead of a misleading 0%.  Open tracking is independent and unaffected.
    EMAIL_CLICK_TRACKING_ENABLED: bool = True
    # How often (minutes) to poll Brevo's transactional events API for
    # delivered/opened/clicked/bounced/spam/unsubscribed.  We poll
    # instead of taking the inbound webhook because the webhook needs a
    # public tunnel + paid plan on some Brevo tiers; polling is free
    # with the regular API key.  Trade-off: up to this many minutes of
    # lag from event-at-Brevo to event-row-in-our-DB.
    BREVO_EVENTS_POLL_INTERVAL_MINUTES: int = 10
    # How often (minutes) to sync Brevo's blocked-contacts list (hard bounces /
    # unsubscribes / spam / admin-blocked) into the suppression list — the
    # backstop to the real-time events poller.  Default every 6 hours.
    BREVO_BLOCKLIST_SYNC_INTERVAL_MINUTES: int = 360
    # Suppress an address once its soft-bounce count reaches this threshold.
    # Soft bounces are transient, but repeated ones hurt sender reputation;
    # default 1 = suppress on the first soft bounce.  Set to 0 to disable
    # (never suppress on soft bounce — Brevo still escalates persistent ones
    # to hard bounces/blocks, which always suppress).
    SOFT_BOUNCE_SUPPRESS_THRESHOLD: int = 1
    # Legacy: the (now-deleted) /webhooks/brevo route used this as a
    # shared-secret gate.  No longer referenced anywhere; keep the
    # setting for one release so anybody whose .env still has it doesn't
    # see a load-time error from pydantic-settings extra=forbid.  Safe
    # to delete in a follow-up after .env templates are scrubbed.
    BREVO_WEBHOOK_SECRET: str = ""

    # Enrichment (optional)
    APOLLO_API_KEY: str = ""
    HUNTER_API_KEY: str = ""
    # Hunter Domain Search result cap. The FREE plan rejects limit > 10 (400);
    # raise on a paid plan.
    HUNTER_DOMAIN_SEARCH_LIMIT: int = 10

    # --- Nonprofit funding discovery (feeds the prospect_signals queue) ---
    # Each poll task no-ops when its *_ENABLED flag is false (default).
    # USAspending grant-award feed (free, no auth).
    USASPENDING_ENABLED: bool = False
    # Trailing window (days) re-scanned each poll.  USAspending action_date
    # data lags reporting by days-to-weeks, so a narrow window misses
    # back-dated awards that only just became visible; 30 covers typical lag.
    # Dedup makes the overlap free.
    USASPENDING_LOOKBACK_DAYS: int = 30
    # Award-size bounds (USD) on the grants we surface.  Big multi-million
    # grants go to large, already-well-funded nonprofits that are a poor
    # outreach fit; cap at $500k by default so we pull the smaller orgs.
    # 0 / None = no bound.  Tunable per-feed in Settings → Discovery.
    USASPENDING_MIN_AWARD_AMOUNT: float | None = None
    USASPENDING_MAX_AWARD_AMOUNT: float | None = 500_000
    # IRS EO BMF new-501(c)(3) feed.  STATES empty = skip (e.g. ["PA","NJ"]).
    IRS_BMF_ENABLED: bool = False
    # NoDecode: keep pydantic-settings from JSON-parsing the env value so
    # the validator below can accept a plain comma-separated string too.
    IRS_BMF_STATES: Annotated[list[str], NoDecode] = []
    IRS_BMF_RULING_LOOKBACK_MONTHS: int = 2

    # --- Contact enrichment + deferred-enrichment queue ---
    # Max NEW (non-deduped) orgs a single discovery poll will enrich.  A
    # 30-day USAspending window returns ~2000 orgs; per-org contact
    # enrichment makes EACH org spend ~1 Anthropic web-search + scrape + 1-3
    # Hunter calls.  Kept conservative (25) so one run can't drain Anthropic
    # credits or trip Hunter's free-tier rate limit (429) in a burst — both
    # observed at 100.  Dedup means each daily run advances through the
    # backlog, so the cap bounds per-run cost without dropping anyone; raise
    # it if you're on paid Anthropic + Hunter tiers.
    FUNDING_DISCOVERY_MAX_PER_RUN: int = 25
    # Max pages the website scraper fetches per org (homepage + a fixed set
    # of contact/about/staff paths).
    FUNDING_SCRAPE_MAX_PAGES: int = 6
    # Backoff (days) between enrichment retries; index = attempt number.
    FUNDING_ENRICHMENT_RETRY_DAYS: list[int] = [7, 30, 60]
    # Give up (mark exhausted) after this many attempts.
    FUNDING_ENRICHMENT_MAX_ATTEMPTS: int = 3
    # Rows processed per daily retry sweep.  Same rate-limit reasoning as
    # FUNDING_DISCOVERY_MAX_PER_RUN — each row re-runs the full enrichment.
    FUNDING_ENRICHMENT_BATCH: int = 25
    # When True, an exhausted org with a mailing address gets a "Direct mail"
    # CRM task (no email, no campaign enrollment) instead of being dropped.
    FUNDING_DIRECT_MAIL_FALLBACK: bool = False

    # --- Signals & Intent Engine v2 collectors --------------------------------
    # Minimum year-over-year drop in 990 contributions/grants revenue to emit a
    # Tier-2 rev_drop signal (0.20 = a 20% decline).
    INTENT_REV_DROP_THRESHOLD: float = 0.20
    # Max monitored orgs processed per ProPublica collector run (politeness +
    # bounded cost; dedup means each run advances the backlog).
    INTENT_PROPUBLICA_MAX_PER_RUN: int = 200
    # Pause between per-org external calls in a collector run (be a good citizen
    # to free public APIs).
    INTENT_COLLECTOR_POLITENESS_SECONDS: float = 0.3
    # Fan-out collectors (Grants.gov RFP / USASpending peer): cap how many
    # monitored orgs a single external event fans out to, and total signals
    # emitted per run (cost + volume control; dedup advances the backlog).
    INTENT_MATCH_MAX_ORGS_PER_EVENT: int = 25
    INTENT_GRANTS_GOV_MAX_PER_RUN: int = 200
    INTENT_USASPENDING_PEER_MAX_PER_RUN: int = 200
    # Only emit new_rfp for opportunities posted within this many days ("new").
    INTENT_RFP_LOOKBACK_DAYS: int = 30
    # Trailing window of recent federal awards scanned for peer_funded.
    INTENT_USASPENDING_LOOKBACK_DAYS: int = 30
    # Skip peer awards above this size (large grants go to big, well-funded
    # orgs — weaker "a comparable peer got funded" signal).  0 = no cap.
    INTENT_USASPENDING_MAX_AWARD_AMOUNT: float = 1_000_000.0
    # Adzuna job-aggregator API — sources the Tier-1 dev_role_posted signal.
    # No key → the collector no-ops (opt-in, like Hunter).  Free tier:
    # https://developer.adzuna.com (register an app → app_id + app_key).
    ADZUNA_APP_ID: str = ""
    ADZUNA_APP_KEY: str = ""
    ADZUNA_COUNTRY: str = "us"
    INTENT_DEV_ROLE_LOOKBACK_DAYS: int = 30
    INTENT_DEV_ROLE_MAX_PER_RUN: int = 200
    # Careers-page check (Track 2): max warm orgs fetched per run (per-org LLM
    # cost; only the high-interest subset is checked).
    INTENT_CAREERS_MAX_ORGS_PER_RUN: int = 50
    # ATS public-board poll (Track 3): max dev_role signals emitted per run.
    INTENT_ATS_MAX_PER_RUN: int = 200
    # On promotion, fire an async task to resolve a recipient contact for the
    # draft lead (domain/scrape/Hunter/ProPublica). Off → drafts stay
    # recipient-less until set by hand / "Find contact".
    INTENT_PROMOTE_ENRICH_CONTACT: bool = True

    # Encryption
    ENCRYPTION_KEY: str = ""

    # IMAP polling
    IMAP_POLL_INTERVAL_MINUTES: int = 20

    # Unipile (hosted LinkedIn browser API on residential IPs).
    # DSN is the tenant host returned from the dashboard, e.g.
    # "api12.unipile.com:13443".  API key from dashboard → access-tokens.
    # Webhook auth: Unipile doesn't HMAC-sign bodies; instead, when creating
    # the webhook you configure a custom HTTP header that Unipile echoes
    # back on every delivery.  We check the inbound request for the same
    # header + value.
    UNIPILE_DSN: str = ""
    UNIPILE_API_KEY: str = ""
    UNIPILE_WEBHOOK_SECRET: str = ""
    UNIPILE_WEBHOOK_AUTH_HEADER: str = "X-Unipile-Auth"
    # Brevo outbound (transactional event) webhook — real-time delivery of
    # delivered/opened/clicked/bounce/spam/unsubscribe/blocked events, on top
    # of the polling backstop.  Brevo doesn't HMAC-sign bodies; like Unipile we
    # use a static shared-secret header (configured on the webhook in Brevo,
    # echoed on every delivery).  Empty secret → the route rejects everything.
    BREVO_WEBHOOK_SECRET: str = ""
    BREVO_WEBHOOK_AUTH_HEADER: str = "X-Brevo-Auth"

    # LinkedIn rate limits.  Unipile manages humanization on its side, but
    # we still enforce daily caps + a per-account min-delay as a burst floor
    # so a runaway sequence can't flood a single account with actions.
    LINKEDIN_DAILY_ACTION_CAP: int = 20         # per-account TOTAL actions / day
    LINKEDIN_MIN_ACTION_DELAY_SECONDS: int = 30 # min gap between actions
    # Sequencer dispatch staggering: the scheduler releases at most one
    # LinkedIn step per account per this many seconds, parking the rest until
    # their slot opens.  This spaces a campaign's leads out (one lead, wait,
    # next lead) instead of dispatching the whole batch at once.  Set to 0 to
    # disable staggering (fall back to the per-action min-delay floor only).
    LINKEDIN_STAGGER_SECONDS: int = 120
    LINKEDIN_POLL_INTERVAL_MINUTES: int = 30    # inbox poller cadence
    # Per-kind subcaps.  LinkedIn's real enforcement: ~100 connects/week for
    # established accounts (~14/day).  20/day is conservative and safe.
    # DMs require 1st-degree — 30/day is the practical ceiling before risk.
    LINKEDIN_DAILY_CONNECT_CAP: int = 20        # per-account connect requests / day
    LINKEDIN_DAILY_DM_CAP: int = 30             # per-account DMs / day
    LINKEDIN_MONTHLY_PAGE_INVITE_CAP: int = 250 # per-PAGE invites / month

    # --- Deliverability guard ---
    # Default for new campaigns' send-time-optimization toggle (per-campaign
    # override lives on the campaign row).
    SEND_TIME_OPTIMIZATION_DEFAULT: bool = False
    # Per-SENDING-DOMAIN caps shared across every campaign that sends from
    # that domain.  Protects domain reputation when several campaigns run
    # at once — the per-campaign caps can't see each other.
    DOMAIN_MAX_PER_HOUR: int = 100
    DOMAIN_MAX_PER_DAY: int = 500
    # Bounce/spam circuit breaker: auto-pause a campaign whose hard-bounce
    # or spam rate crosses the threshold over the window.  MIN_SAMPLE stops
    # one unlucky bounce in the first handful of sends from tripping it.
    CIRCUIT_BREAKER_ENABLED: bool = True
    CIRCUIT_BREAKER_MIN_SAMPLE: int = 20
    CIRCUIT_BREAKER_BOUNCE_RATE: float = 0.05   # 5% hard-bounce → pause
    CIRCUIT_BREAKER_SPAM_RATE: float = 0.001    # 0.1% spam → pause
    CIRCUIT_BREAKER_WINDOW_HOURS: int = 24

    # --- Agent / notifications ---
    # Where agent alerts (positive-reply pings, task reminders, the daily
    # digest) are emailed.  Empty = notifications persist in the DB but
    # no email goes out.
    OWNER_NOTIFY_EMAIL: str = ""
    OWNER_NOTIFY_NAME: str = "Operator"
    # The FROM address for agent alert emails.  Defaults (empty) fall back
    # to the campaign Brevo sender, but agent alerts are internal mail to
    # the operator, so they read better from your own address rather than
    # whatever name campaigns send under.  Must be a verified Brevo sender.
    OWNER_NOTIFY_FROM_EMAIL: str = ""
    OWNER_NOTIFY_FROM_NAME: str = ""
    # Master kill-switch for every autonomous agent behaviour (reply
    # classification, reminders, nudges, digest).  The finer-grained
    # per-behaviour toggles live in the runtime-editable AgentSettings
    # DB row; this env var is the hard off-switch that wins over all of
    # them — useful for incident response without touching the DB.
    AGENT_ENABLED: bool = True
    # Classification (reply sentiment/intent) is an extraction task —
    # Haiku handles it fine and runs per inbound reply, so cost matters.
    ANTHROPIC_AGENT_MODEL: str = "claude-haiku-4-5-20251001"
    # Reply DRAFTS are prose the user may actually send — Sonnet quality.
    # Only invoked when AgentSettings.auto_draft_replies is on.
    ANTHROPIC_AGENT_DRAFT_MODEL: str = "claude-sonnet-4-6"
    AGENT_REMINDER_SWEEP_INTERVAL_MINUTES: int = 30
    # Hour (UTC) the daily digest email goes out.
    AGENT_DIGEST_HOUR_UTC: int = 12
    # An open opportunity with no activity for this many days gets a
    # nudge task + notification.
    AGENT_STALE_OPP_DAYS: int = 7
    # Tasks due within this window trigger a "due soon" reminder.
    AGENT_TASK_DUE_SOON_HOURS: int = 24

    # App
    SECRET_KEY: str = "dev-secret-change-me"
    FRONTEND_URL: str = "http://localhost:5173"
    WEBHOOK_BASE_URL: str = "http://localhost:8000"

    # --- Tenancy (multi-tenancy Phase 2) ---
    # Non-owner runtime DSN (role ``app_user`` from scripts/bootstrap_db.sql).
    # Table owners BYPASS row-level security, so once RLS enforcement lands
    # the API + workers must connect via this role.  Empty = fall back to
    # DATABASE_URL (owner) — fine until policies are enabled.
    APP_DATABASE_URL: str = ""

    # --- Hardening (multi-tenancy Phase 7) ---
    # Retired Fernet keys (comma-separated) still accepted for DECRYPTION
    # during a rotation; new encryptions always use ENCRYPTION_KEY.  See
    # services/encryption.py for the rotation runbook.
    ENCRYPTION_KEYS_OLD: str = ""
    # Per-tenant API request budget per minute (0 disables).  Fail-open on
    # Redis outages.
    API_RATE_LIMIT_PER_MINUTE: int = 600
    # Comma-separated emails allowed into the /admin surface.
    SUPERADMIN_EMAILS: str = ""
    # When True, tenants with NULL subscription_status (legacy/unbilled,
    # pre-Stripe rows) are DENIED spend instead of exempt — flip this on
    # hosted deployments once every real tenant is on a plan.
    BILLING_REQUIRE_SUBSCRIPTION: bool = False

    # --- Billing / Stripe (multi-tenancy Phase 5) ---
    # Empty = billing disabled: checkout/portal 400, entitlements stay
    # permissive for legacy/unbilled tenants.  Webhook secret verifies
    # Stripe signatures (stripe listen / dashboard endpoint secret).
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_AGENCY: str = ""
    BILLING_TRIAL_DAYS: int = 14
    # past_due keeps read access; spend is denied this many days after
    # current_period_end, then effectively canceled.
    BILLING_GRACE_DAYS: int = 7

    # --- Auth / sessions (multi-tenancy Phase 1) ---
    SESSION_TTL_DAYS: int = 30
    # Set True in production (HTTPS).  False default keeps the cookie
    # working on plain-http localhost dev.
    SESSION_COOKIE_SECURE: bool = False
    # Platform transactional email — auth mail only (password resets).
    # Distinct from the tenant/campaign Brevo credentials: a locked-out
    # user has no tenant context, so auth mail can't ride tenant keys.
    # Empty = reset links are created but no email goes out (dev).
    PLATFORM_BREVO_API_KEY: str = ""
    PLATFORM_SENDER_EMAIL: str = ""
    PLATFORM_SENDER_NAME: str = "OutboundOS"

    @field_validator("IRS_BMF_STATES", mode="before")
    @classmethod
    def _split_states(cls, v: object) -> object:
        """Accept a comma-separated env string (``PA,NJ,NY``) OR a JSON
        list (``["PA","NJ"]``).  The field is annotated ``NoDecode``, so
        the raw env string arrives here undecoded and we parse it."""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                import json
                try:
                    return json.loads(s)
                except ValueError:
                    return []
            return [part.strip() for part in s.split(",") if part.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


class ConfigurationError(RuntimeError):
    """Raised by ``validate_required_settings`` when a load-bearing env
    var is empty or set to a known-bad default.  Surfaced at app boot so
    a half-configured ``.env`` fails immediately rather than mid-campaign.
    """


# Settings whose absence we deliberately tolerate but loudly warn about.
# These either have safe defaults (`FRONTEND_URL`, `WEBHOOK_BASE_URL`) or
# only matter for optional features (`UNIPILE_*`, `APOLLO_*`, `HUNTER_*`).
_HARD_REQUIRED = (
    # BYOK (multi-tenancy Phase 4): provider keys (Anthropic/Brevo/Apollo/
    # Hunter/Unipile) are PER-TENANT rows now, entered in Settings — the
    # platform boots without any of them.  Only the platform's own secrets
    # remain hard requirements.
    "ENCRYPTION_KEY",
    "SECRET_KEY",
)
_DANGEROUS_DEFAULTS = {
    "SECRET_KEY": "dev-secret-change-me",
}


def validate_required_settings(*, raise_on_missing: bool = True) -> list[str]:
    """Inspect the loaded settings and return a list of human-readable
    error strings (``[]`` when everything's healthy).  Called from
    ``app.main`` at startup; raises on missing hard requirements so the
    container fails fast at ``docker compose up`` instead of running for
    minutes and only erroring at first send/compose attempt.

    Returns the error list either way (test-friendly), and raises
    ``ConfigurationError`` when ``raise_on_missing=True`` (the default).
    """
    errors: list[str] = []
    for key in _HARD_REQUIRED:
        val = getattr(settings, key, "")
        if not val or not str(val).strip():
            errors.append(
                f"{key} is empty.  Set it in .env before starting the backend."
            )
            continue
        bad = _DANGEROUS_DEFAULTS.get(key)
        if bad is not None and str(val) == bad:
            errors.append(
                f"{key} is still set to the insecure default ({bad!r}). "
                "Replace it in .env before sending any real campaign."
            )
    if errors and raise_on_missing:
        raise ConfigurationError(
            "Refusing to start with missing / insecure-default config:\n  - "
            + "\n  - ".join(errors)
        )
    return errors
