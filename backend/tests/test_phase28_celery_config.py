"""Sanity checks on the Celery broker config.

We've been bitten once by a worker restart leaving 83 ``send_lead`` tasks
orphaned in Redis ``unacked`` for an hour (the default visibility
timeout).  This module asserts the knobs that prevent that recurrence
stay set.
"""
from __future__ import annotations

from app.workers.celery_app import celery_app


def test_visibility_timeout_is_short_for_fast_recovery():
    """A future-ETA task whose worker dies before the ETA needs the
    visibility timeout to expire before any other worker reclaims it.
    The Celery default is 1 hour, which is too long for ``countdown=60``
    rate-limit retries — set it to 5 min."""
    opts = celery_app.conf.broker_transport_options or {}
    assert opts.get("visibility_timeout") == 300, (
        "broker_transport_options.visibility_timeout must be 300 (5 min) "
        "to recover orphaned ETA tasks within a reasonable window. "
        f"Got: {opts!r}"
    )


def test_task_acks_late_is_on():
    """Without acks-late, a worker that crashes mid-task leaves the task
    permanently consumed and the lead's send_lead never runs again.
    Acks-late + visibility_timeout is the only way Redis-broker Celery
    recovers from worker death."""
    assert celery_app.conf.task_acks_late is True


def test_prefetch_multiplier_is_one():
    """A single worker would otherwise prefetch dozens of tasks and hold
    them in memory; a crash would orphan all of them.  Prefetch=1 keeps
    the blast radius bounded to one task per fork pool worker."""
    assert celery_app.conf.worker_prefetch_multiplier == 1
