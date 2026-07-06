"""Celery tasks for the Signals & Intent Engine v2 collectors.

``intent.backfill_orgs``  — seed/refresh the monitored-org set from existing
                            EIN-bearing data (daily; cheap + idempotent).
``intent.collect_propublica_rev_delta`` — ProPublica 990 grant-revenue-delta
                            collector → Tier-2 ``rev_drop`` signals (weekly;
                            990 data moves slowly).

``acks_late=False`` so a long run is never redelivered into a duplicate
(collectors are idempotent via the unique ``dedupe_key`` regardless, and a
run lost to a crash simply re-runs next tick).  Grants.gov + USASpending
collectors land in this module in the next sub-phase, after the gate.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import Lead
from app.services.intent import (
    collect_ats, collect_careers, collect_dev_roles, collect_grants_gov,
    collect_propublica, collect_usaspending, enrich, orgs, promote, scoring,
)
from app.workers.celery_app import celery_app
from app.tenancy.context import with_default_tenant, with_record_tenant

logger = logging.getLogger(__name__)


async def _backfill_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await orgs.backfill_orgs_from_existing(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.backfill_orgs", acks_late=False)
def backfill_orgs() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_backfill_async))


async def _propublica_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await collect_propublica.collect_propublica_rev_delta(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_propublica_rev_delta", acks_late=False)
def collect_propublica_rev_delta() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_propublica_async))


async def _recompute_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await scoring.recompute_all_intent(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.recompute_intent", acks_late=False)
def recompute_intent() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_recompute_async))


def _profile_lists(profile) -> tuple[list[str], list[str]]:
    """(cause_prefixes, geographies) from an ICP intent profile."""
    causes = [str(c) for c in (profile.cause_codes or [])]
    geos = [str(g) for g in (profile.geographies or [])]
    return causes, geos


def _rfp_keywords(profile) -> list[str]:
    """Grants.gov search keywords for the profile (Phase-5 ``rfp_keywords``);
    falls back to the cause-code list when none are set."""
    kws = profile.rfp_keywords or []
    if kws:
        return [str(k) for k in kws]
    return [str(c) for c in (profile.cause_codes or [])]


async def _collect_grants_gov_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            profile = await scoring.get_active_profile(session)
            if profile is None:
                logger.info("intent.collect_grants_gov: no active ICP profile — skipping")
                return {"skipped": "no_active_profile"}
            causes, geos = _profile_lists(profile)
            keywords = _rfp_keywords(profile)
            if not keywords:
                logger.info("intent.collect_grants_gov: profile has no keywords — skipping")
                return {"skipped": "no_keywords"}
            return await collect_grants_gov.collect_grants_gov(
                session, keywords=keywords, cause_prefixes=causes, geographies=geos)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_grants_gov", acks_late=False)
def collect_grants_gov_task() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_collect_grants_gov_async))


async def _collect_dev_roles_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            profile = await scoring.get_active_profile(session)
            causes = geos = None
            if profile is not None:
                causes, geos = _profile_lists(profile)
            return await collect_dev_roles.collect_dev_roles(
                session, cause_prefixes=causes, geographies=geos)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_dev_roles", acks_late=False)
def collect_dev_roles_task() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_collect_dev_roles_async))


async def _collect_careers_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await collect_careers.collect_careers_dev_roles(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_careers_dev_roles", acks_late=False)
def collect_careers_dev_roles_task() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_collect_careers_async))


async def _collect_ats_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await collect_ats.collect_ats_dev_roles(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_ats_dev_roles", acks_late=False)
def collect_ats_dev_roles_task() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_collect_ats_async))


async def _collect_usaspending_peer_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            profile = await scoring.get_active_profile(session)
            if profile is None:
                logger.info("intent.collect_usaspending_peer: no active ICP profile — skipping")
                return {"skipped": "no_active_profile"}
            causes, geos = _profile_lists(profile)
            return await collect_usaspending.collect_usaspending_peer(
                session, cause_prefixes=causes, geographies=geos)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.collect_usaspending_peer", acks_late=False)
def collect_usaspending_peer_task() -> dict[str, int]:
    return asyncio.run(with_default_tenant(_collect_usaspending_peer_async))


async def _promote_async() -> dict[str, int]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await promote.promote_eligible(session)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.promote_eligible", acks_late=False)
def promote_eligible() -> dict[str, int]:
    """Stage approval-pending DRAFTS for promotable orgs.  NEVER sends."""
    return asyncio.run(with_default_tenant(_promote_async))


async def _enrich_draft_async(lead_id: str) -> dict:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await enrich.enrich_draft_lead(session, lead_id)
    finally:
        await engine.dispose()


@celery_app.task(name="intent.enrich_draft_contact", acks_late=False)
def enrich_draft_contact(lead_id: str) -> dict:
    """Resolve + attach a recipient to a promoted draft lead.  NEVER sends."""
    return asyncio.run(with_record_tenant(Lead, lead_id, _enrich_draft_async, lead_id))
