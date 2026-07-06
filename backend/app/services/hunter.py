"""Hunter.io email verification.

When the key isn't configured we return a permissive default so the send
worker doesn't drop leads — verification is opt-in.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.services.tenant_keys import ambient_api_key

logger = logging.getLogger(__name__)

HUNTER_URL = "https://api.hunter.io/v2/email-verifier"
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"
_DELIVERABLE_STATUSES = {"valid", "accept_all", "webmail"}


async def verify_email_hunter(email: str) -> dict[str, Any]:
    api_key = await ambient_api_key("hunter")
    if not api_key:
        return {"deliverable": True, "score": 100}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                HUNTER_URL,
                params={"email": email, "api_key": api_key},
            )
            resp.raise_for_status()
            payload = (resp.json() or {}).get("data") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Hunter verify failed for %s: %s", email, e)
        # Permissive fallback — never block sending on a verifier outage.
        return {"deliverable": True, "score": 0}

    return {
        "deliverable": (payload.get("status") or "").lower() in _DELIVERABLE_STATUSES,
        "score": int(payload.get("score") or 0),
    }


async def domain_search(domain: str) -> list[dict[str, Any]]:
    """Hunter Domain Search — every email Hunter has for ``domain``, with
    role/type metadata.  The right tool when starting from an ORG (not a
    person): no target name needed.

    Returns ``[{email, first_name, last_name, position, type}]`` or ``[]``
    when no key is set / no domain / the API hard-fails (permissive — an
    outage must never crash the feed).
    """
    api_key = await ambient_api_key("hunter")
    if not api_key or not domain:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # The free Hunter plan rejects limit > 10 with a 400, so this is
            # capped (configurable for paid plans via HUNTER_DOMAIN_SEARCH_LIMIT).
            resp = await client.get(HUNTER_DOMAIN_URL, params={
                "domain": domain, "api_key": api_key,
                "limit": settings.HUNTER_DOMAIN_SEARCH_LIMIT,
            })
            resp.raise_for_status()
            emails = ((resp.json() or {}).get("data") or {}).get("emails") or []
    except Exception as e:  # noqa: BLE001 — opt-in feature, never crash the feed
        logger.warning("Hunter domain_search failed for %s: %s", domain, e)
        return []

    out: list[dict[str, Any]] = []
    for e in emails:
        value = (e.get("value") or "").strip()
        if not value:
            continue
        out.append({
            "email": value,
            "first_name": e.get("first_name"),
            "last_name": e.get("last_name"),
            "position": e.get("position"),
            "type": e.get("type"),   # 'personal' | 'generic'
        })
    return out


async def find_email_hunter(
    domain: str,
    *,
    full_name: str | None = None,
    role: str | None = None,
) -> dict[str, Any] | None:
    """Find a contact email at ``domain`` (Hunter Email Finder when a
    ``full_name`` is known, else Domain Search ranked toward ``role``).

    Returns ``{email, first_name, last_name, title, score, generic}`` or
    ``None`` when no key is set / no match / the API hard-fails.  Hunter
    is opt-in: without ``HUNTER_API_KEY`` this returns None, so the
    discovery worker falls back to the notification-only path.
    """
    api_key = await ambient_api_key("hunter")
    if not api_key or not domain:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if full_name:
                resp = await client.get(HUNTER_FINDER_URL, params={
                    "domain": domain, "full_name": full_name,
                    "api_key": api_key,
                })
                resp.raise_for_status()
                d = (resp.json() or {}).get("data") or {}
                if not d.get("email"):
                    return None
                return {
                    "email": d["email"],
                    "first_name": d.get("first_name"),
                    "last_name": d.get("last_name"),
                    "title": d.get("position"),
                    "score": int(d.get("score") or 0),
                    "generic": False,
                }

            # No name → domain search, then rank toward the wanted role.
            # The free Hunter plan rejects limit > 10 with a 400, so this is
            # capped (configurable for paid plans via HUNTER_DOMAIN_SEARCH_LIMIT).
            resp = await client.get(HUNTER_DOMAIN_URL, params={
                "domain": domain, "api_key": api_key,
                "limit": settings.HUNTER_DOMAIN_SEARCH_LIMIT,
            })
            resp.raise_for_status()
            emails = ((resp.json() or {}).get("data") or {}).get("emails") or []
    except Exception as e:  # noqa: BLE001 — feature is opt-in; never crash the feed
        logger.warning("Hunter find failed for %s: %s", domain, e)
        return None

    if not emails:
        return None

    role_kw = (role or "").lower()

    def _rank(e: dict[str, Any]) -> tuple[int, int, int]:
        position = (e.get("position") or "").lower()
        role_match = 1 if role_kw and any(w in position for w in role_kw.split()) else 0
        personal = 1 if (e.get("type") == "personal") else 0
        return (role_match, personal, int(e.get("confidence") or 0))

    best = max(emails, key=_rank)
    if not best.get("value"):
        return None
    return {
        "email": best["value"],
        "first_name": best.get("first_name"),
        "last_name": best.get("last_name"),
        "title": best.get("position"),
        "score": int(best.get("confidence") or 0),
        "generic": best.get("type") == "generic",
    }
