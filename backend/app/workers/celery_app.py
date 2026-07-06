from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "emailblaster",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.ingest",
        "app.workers.research",
        "app.workers.compose",
        "app.workers.send",
        "app.workers.reply_poller",
        "app.workers.sequencer",
        "app.workers.linkedin_poller",
        "app.workers.brevo_events_poller",
        "app.workers.brevo_blocklist_sync",
        "app.workers.lead_sweeper",
        "app.workers.social_listening",
        "app.workers.agent_sweeper",
        "app.workers.digest",
        "app.workers.deliverability",
        "app.workers.copy_insights_refresher",
        "app.workers.signals",
        "app.workers.icp",
        "app.workers.funding_signals",
        "app.workers.intent_collectors",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Celery on the Redis broker holds future-ETA tasks in worker
    # process MEMORY until the ETA arrives.  If the worker dies before
    # then, the task sits in ``unacked`` until visibility_timeout
    # expires and a live worker reclaims it.  The default is 1 hour;
    # 5 min is much friendlier when we ``apply_async(countdown=60)`` for
    # rate-limit retries.  Side note: the timeout MUST be >= the
    # longest realistic task runtime, or in-progress tasks can get
    # double-delivered.  300 s comfortably exceeds any task we run
    # (longest is Brevo POST + DB write, well under 30 s).
    broker_transport_options={"visibility_timeout": 300},
)

celery_app.conf.beat_schedule = {
    "poll-all-replies": {
        "task": "reply_poller.poll_all_replies",
        "schedule": float(settings.IMAP_POLL_INTERVAL_MINUTES * 60),
    },
    "advance-sequences": {
        "task": "sequencer.advance_sequences",
        "schedule": 60.0,
    },
    "pace-first-emails": {
        # Paced dispatcher for the legacy first email: feeds each running
        # campaign's pending backlog at the rate the caps allow, instead of
        # every lead self-re-enqueuing (which storms the broker).
        "task": "send.pace_first_emails",
        "schedule": 60.0,
    },
    "linkedin-poll": {
        "task": "linkedin_poller.poll_all",
        "schedule": float(settings.LINKEDIN_POLL_INTERVAL_MINUTES * 60),
    },
    "brevo-events-poll": {
        "task": "brevo_events_poller.poll",
        "schedule": float(settings.BREVO_EVENTS_POLL_INTERVAL_MINUTES * 60),
    },
    "brevo-blocklist-sync": {
        "task": "brevo_blocklist_sync.sync",
        "schedule": float(settings.BREVO_BLOCKLIST_SYNC_INTERVAL_MINUTES * 60),
    },
    "lead-sweeper": {
        "task": "lead_sweeper.sweep_stale",
        "schedule": 300.0,  # every 5 min
    },
    "social-radar-runner": {
        # Dispatcher that selects social listening searches whose
        # ``next_run_at <= now`` and enqueues ``run_social_search`` for
        # each.  Per-search frequency lives on the search row.
        "task": "social_listening.scheduled_runner",
        "schedule": 60.0,
    },
    "agent-sweep-reminders": {
        # Owner reminders for open tasks due soon / overdue.  One
        # reminder per task ever (CrmActivity.reminder_sent_at anchor).
        "task": "agent_sweeper.sweep_reminders",
        "schedule": float(settings.AGENT_REMINDER_SWEEP_INTERVAL_MINUTES * 60),
    },
    "agent-sweep-stale-opps": {
        # Nudge open opportunities idle > AGENT_STALE_OPP_DAYS.
        "task": "agent_sweeper.sweep_stale_opps",
        "schedule": 3600.0,  # hourly
    },
    "signals-runner": {
        # Dispatcher for due prospect-signal watches (job change /
        # funding / hiring) — sibling of social-radar-runner.
        "task": "signals.scheduled_runner",
        "schedule": 60.0,
    },
    "copy-insights-refresh": {
        # Per-campaign winning-angle summaries; LLM runs only for
        # campaigns with enough NEW reply outcomes since last refresh.
        "task": "copy_insights.refresh_all",
        "schedule": 3600.0,  # hourly
    },
    "deliverability-health-sweep": {
        # Bounce/spam circuit-breaker backstop over running campaigns.
        "task": "deliverability.sweep_health",
        "schedule": 900.0,  # every 15 min
    },
    "icp-refresh-profile": {
        # Regenerate the auto ICP from closed-won deals.
        "task": "icp.refresh_profile",
        "schedule": crontab(minute=0, hour=2),  # daily, 02:00 UTC
    },
    "icp-discover": {
        # Stage new lookalike candidates an hour after the refresh.
        "task": "icp.discover",
        "schedule": crontab(minute=0, hour=3),  # daily, 03:00 UTC
    },
    "agent-daily-digest": {
        # One summary email a day; idempotent via the digest:<date>
        # dedup key, so a beat double-fire can't send two.
        "task": "digest.send_daily",
        "schedule": crontab(minute=0, hour=settings.AGENT_DIGEST_HOUR_UTC),
    },
    "funding-poll-usaspending": {
        # Recent nonprofit grant awards → prospect_signals.  No-ops
        # unless USASPENDING_ENABLED.
        "task": "funding.poll_usaspending",
        "schedule": crontab(minute=0, hour=4),  # daily, 04:00 UTC
    },
    "funding-poll-irs-bmf": {
        # New 501(c)(3) rulings → prospect_signals.  No-ops unless
        # IRS_BMF_ENABLED + states configured.  Runs the 15th, after the
        # 2nd-Tuesday EO BMF refresh.
        "task": "funding.poll_irs_bmf",
        "schedule": crontab(minute=0, hour=5, day_of_month=15),
    },
    "funding-retry-enrichment": {
        # Re-attempt contact resolution for orgs parked in the deferred
        # enrichment queue; promote the ones that now resolve.
        "task": "funding.retry_enrichment",
        "schedule": crontab(minute=0, hour=6),  # daily, 06:00 UTC
    },
    # --- Signals & Intent Engine v2 collectors ---
    "intent-backfill-orgs": {
        # Seed/refresh the monitored-org set from EIN-bearing data.  Cheap +
        # idempotent (no external calls); keeps orgs fresh from new discovery.
        "task": "intent.backfill_orgs",
        "schedule": crontab(minute=30, hour=3),  # daily, 03:30 UTC
    },
    "intent-collect-propublica": {
        # ProPublica 990 grant-revenue-delta → Tier-2 rev_drop signals.
        # Weekly — 990 financials change only when new filings post.
        "task": "intent.collect_propublica_rev_delta",
        "schedule": crontab(minute=0, hour=7, day_of_week=1),  # Mondays 07:00 UTC
    },
    "intent-collect-grants-gov": {
        # New federal RFPs matching the active ICP cause → Tier-1 new_rfp.
        # Daily — opportunities post continuously and carry deadlines.
        # No-ops until an ICP intent profile is active (Phase 5).
        "task": "intent.collect_grants_gov",
        "schedule": crontab(minute=0, hour=6),  # daily, 06:00 UTC
    },
    "intent-collect-usaspending-peer": {
        # Recent peer nonprofit federal awards in monitored states → Tier-2
        # peer_funded.  Daily.  No-ops until an ICP profile is active.
        "task": "intent.collect_usaspending_peer",
        "schedule": crontab(minute=30, hour=6),  # daily, 06:30 UTC
    },
    "intent-collect-dev-roles": {
        # Posted dev/grant roles at monitored nonprofits → Tier-1
        # dev_role_posted (Adzuna).  Daily.  No-ops without an Adzuna key.
        "task": "intent.collect_dev_roles",
        "schedule": crontab(minute=45, hour=6),  # daily, 06:45 UTC
    },
    "intent-collect-ats": {
        # Public ATS boards (Greenhouse/Lever/Ashby) for ATS-configured orgs →
        # Tier-1 dev_role_posted (Track 3).  Daily.
        "task": "intent.collect_ats_dev_roles",
        "schedule": crontab(minute=50, hour=6),  # daily, 06:50 UTC
    },
    "intent-collect-careers": {
        # Careers-page dev-role check over the warm-org subset → Tier-1
        # dev_role_posted (Track 2).  Daily, bounded per run.
        "task": "intent.collect_careers_dev_roles",
        "schedule": crontab(minute=0, hour=7),  # daily, 07:00 UTC
    },
    "intent-recompute": {
        # Time-decay + ICP-weight + org-fit roll-up over every org with signals.
        # Daily — decay advances daily even when no new signals land, and the
        # collectors above refresh daily.
        "task": "intent.recompute_intent",
        "schedule": crontab(minute=30, hour=7),  # daily, 07:30 UTC (after collectors)
    },
    "intent-promote-eligible": {
        # Stage approval-pending DRAFTS for orgs over the ICP promotion
        # threshold.  NEVER sends — a human approves through the normal flow.
        "task": "intent.promote_eligible",
        "schedule": crontab(minute=0, hour=8),  # daily, 08:00 UTC (after recompute)
    },
}
