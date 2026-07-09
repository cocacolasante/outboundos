"""Resolve a decision-maker contact for a discovered nonprofit.

Cheapest-first pipeline, short-circuiting on the first deliverable
contact (see the feature design notes):

  a) DOMAIN — the org's own website if known, else ONE capped Haiku
     web-search for the official site (aggregator hosts rejected).  No
     domain → ``no_domain`` (the org goes to the deferred queue).
  b) WEBSITE SCRAPE — fetch the homepage + a small fixed set of contact/
     about/staff pages and extract on-domain emails.  Small nonprofits
     publish staff emails openly, so this is the PRIMARY (free) path.
  c) HUNTER domain-search — fallback when the scrape finds nothing.
  d) PROPUBLICA (optional) — an officer name (established 990-filers
     only) → Hunter email-finder.
  e) CHOOSE by role priority (ED → Development → Grants → any named
     person → generic info@ LAST) and require Hunter-verified delivery.

Returns a structured ``ContactResult``; ``status`` is
``resolved`` | ``no_domain`` | ``no_contact``.  Never raises — every
network step soft-fails so the beat can't be crashed by an outage.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services import hunter
from app.services._anthropic import (
    extract_text,
    get_client,
    parse_json_array,
)
from app.services.tenant_keys import ambient_api_key
from app.services.funding_sources import propublica
from app.services.funding_sources.base import DiscoveredOrg
# Reuse the existing capped web-lookup pattern (Haiku, cost-wrapped).
from app.services.signal_detection import _web_lookup

logger = logging.getLogger(__name__)

# Decision-maker roles in outreach priority order (also the Hunter role hint).
_ROLE_PRIORITY = ["Executive Director", "Development Director", "Grants Manager"]
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TAG_RE = re.compile(r"<[^>]+>")

# Hosts that are NOT an org's own site — reject as discovered domains.
_AGGREGATOR_HOSTS = {
    "facebook.com", "fb.com", "linkedin.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "youtu.be", "tiktok.com", "guidestar.org",
    "candid.org", "charitynavigator.org", "propublica.org", "irs.gov",
    "wikipedia.org", "eventbrite.com", "gofundme.com", "givebutter.com",
    "donorbox.org", "classy.org", "patreon.com", "meetup.com", "yelp.com",
    "google.com", "goo.gl", "bit.ly", "wordpress.com", "blogspot.com",
    "amazonaws.com", "godaddy.com", "wixsite.com", "mailchi.mp",
}
# Generic mailbox local-parts — kept, but ranked LAST.
_GENERIC_LOCALS = {
    "info", "contact", "hello", "admin", "office", "mail", "general",
    "inquiries", "support", "help", "team", "hi", "outreach",
}
# Contact-bearing paths small nonprofits commonly publish staff emails on.
_SCRAPE_PATHS = [
    "", "/contact", "/contact-us", "/about", "/about-us",
    "/staff", "/team", "/our-team", "/leadership", "/people",
]


@dataclass
class ContactResult:
    status: str   # 'resolved' | 'no_domain' | 'no_contact'
    domain: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    via: str | None = None   # 'website' | 'hunter_domain_search' | 'propublica'


def _domain_from_website(website: str | None) -> str | None:
    if not website:
        return None
    host = _SCHEME_RE.sub("", website.strip()).split("/", 1)[0].strip().lower()
    host = host.split("@")[-1].split(":")[0]          # strip any user@ / :port
    host = host[4:] if host.startswith("www.") else host
    return host or None


def _is_aggregator(host: str | None) -> bool:
    if not host:
        return True
    h = host.lower()
    return any(h == agg or h.endswith("." + agg) for agg in _AGGREGATOR_HOSTS)


def _email_on_domain(email: str, domain: str) -> bool:
    host = email.rsplit("@", 1)[-1].lower()
    return host == domain or host.endswith("." + domain)


async def _discover_domain(org: DiscoveredOrg) -> str | None:
    """ONE capped Haiku web-search for the org's official website domain.
    Rejects social / aggregator hosts."""
    web = await _web_lookup(
        f'"{org.org_name}" {org.state or ""} official website nonprofit'.strip(),
        'What is the official website domain of this nonprofit organization '
        "(its OWN site, not Facebook / LinkedIn / GuideStar / a directory)? "
        'Respond ONLY with JSON: {"domain": "example.org or empty string"}.',
    )
    domain = _domain_from_website((web or {}).get("domain"))
    if domain and _is_aggregator(domain):
        return None
    return domain


async def _pair_contacts_llm(
    page_text: str, emails: list[str],
) -> list[dict[str, Any]]:
    """One capped Haiku call to PAIR (not decide) names/titles to the
    emails already scraped off the page.  Falls back to bare emails (no
    names) on any failure — so the scrape still yields candidates."""
    bare = [{"email": e, "first_name": None, "last_name": None, "title": None}
            for e in emails]
    if not emails or not page_text.strip():
        return bare
    prompt = (
        "Below is text scraped from a nonprofit's website and a list of email "
        "addresses found on it.  For EACH email, if the page clearly states the "
        "person's name and/or job title, pair them.  Do NOT invent anything — "
        "leave a field null if the page doesn't say.  Only use emails from the "
        "given list.\n\n"
        f"EMAILS: {emails}\n\nPAGE TEXT:\n{page_text[:6000]}\n\n"
        'Respond ONLY with a JSON array: '
        '[{"email": "...", "first_name": "...|null", "last_name": "...|null", '
        '"title": "...|null"}].'
    )
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_AGENT_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = parse_json_array(extract_text(message))
    except Exception as e:  # noqa: BLE001 — pairing is best-effort
        logger.warning("contact pairing LLM failed: %s", e)
        return bare
    if not parsed:
        return bare
    allowed = {e.lower() for e in emails}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parsed:
        if not isinstance(row, dict):
            continue
        email = (row.get("email") or "").strip().lower()
        if email not in allowed or email in seen:
            continue
        seen.add(email)
        out.append({
            "email": email,
            "first_name": (row.get("first_name") or None),
            "last_name": (row.get("last_name") or None),
            "title": (row.get("title") or None),
        })
    # Include any scraped email the model dropped, as a bare candidate.
    for e in emails:
        if e.lower() not in seen:
            out.append({"email": e, "first_name": None, "last_name": None, "title": None})
    return out


async def _scrape_contacts(domain: str) -> list[dict[str, Any]]:
    """Fetch the homepage + a fixed set of contact/about/staff paths, pull
    on-domain emails, and pair names/titles via one Haiku call.  Returns
    ``[{email, first_name, last_name, title, via, generic}]`` (may be empty).
    """
    max_pages = max(1, int(settings.FUNDING_SCRAPE_MAX_PAGES))
    found_emails: list[str] = []
    seen_emails: set[str] = set()
    text_chunks: list[str] = []
    base = f"https://{domain}"
    try:
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (outboundos contact discovery)"},
        ) as client:
            for path in _SCRAPE_PATHS[:max_pages]:
                url = base + path
                try:
                    resp = await client.get(url)
                    if resp.status_code >= 400:
                        continue
                    ctype = resp.headers.get("content-type", "")
                    if "html" not in ctype.lower() and "text" not in ctype.lower():
                        continue
                    body = resp.text
                except Exception:  # noqa: BLE001 — one bad page must not abort
                    continue
                text_chunks.append(_TAG_RE.sub(" ", body))
                for m in _EMAIL_RE.findall(body):
                    addr = m.strip().lower().rstrip(".")
                    if addr in seen_emails or not _email_on_domain(addr, domain):
                        continue
                    # Skip obvious asset filenames mis-parsed as emails.
                    if addr.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
                        continue
                    seen_emails.add(addr)
                    found_emails.append(addr)
    except Exception as e:  # noqa: BLE001 — scrape is best-effort
        logger.warning("scrape failed for %s: %s", domain, e)
        return []

    if not found_emails:
        return []

    paired = await _pair_contacts_llm("\n".join(text_chunks), found_emails)
    out: list[dict[str, Any]] = []
    for c in paired:
        local = c["email"].split("@", 1)[0]
        out.append({
            "email": c["email"],
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "title": c.get("title"),
            "via": "website",
            "generic": local in _GENERIC_LOCALS,
        })
    return out


def _role_rank(cand: dict[str, Any]) -> tuple[int, int]:
    """Sort key (higher = preferred): role tier, then having a name.

    Tiers: 5 ED/CEO/President · 4 Development/Advancement · 3 Grants ·
    2 any named person · 0 generic mailbox (LAST)."""
    title = (cand.get("title") or "").lower()
    local = cand["email"].split("@", 1)[0].lower()
    is_generic = bool(cand.get("generic")) or local in _GENERIC_LOCALS
    has_name = 1 if (cand.get("first_name") or cand.get("last_name")) else 0
    if is_generic:
        return (0, has_name)
    if any(k in title for k in ("executive director", "ceo", "chief executive", "president", "founder")):
        tier = 5
    elif any(k in title for k in ("development", "advancement")):
        tier = 4
    elif "grant" in title:
        tier = 3
    elif has_name or title:
        tier = 2
    else:
        tier = 1
    return (tier, has_name)


async def resolve_contact(org: DiscoveredOrg) -> ContactResult:
    """Resolve a deliverable decision-maker contact for ``org``."""
    # a) Domain.
    domain = _domain_from_website(org.website)
    if domain and _is_aggregator(domain):
        domain = None
    if not domain:
        domain = await _discover_domain(org)
    if not domain:
        return ContactResult(status="no_domain")

    candidates: list[dict[str, Any]] = []

    # b) Website scrape (primary).
    candidates.extend(await _scrape_contacts(domain))

    # c) Hunter domain-search (fallback when the scrape found nothing).
    if not candidates:
        for e in await hunter.domain_search(domain):
            local = e["email"].split("@", 1)[0].lower()
            candidates.append({
                "email": e["email"].strip().lower(),
                "first_name": e.get("first_name"),
                "last_name": e.get("last_name"),
                "title": e.get("position"),
                "via": "hunter_domain_search",
                "generic": (e.get("type") == "generic") or local in _GENERIC_LOCALS,
            })

    # d) ProPublica assist (established 990-filers only) → email-finder.
    if not candidates and org.ein:
        prof = await propublica.lookup_org(org.ein)
        if prof and prof.get("first_name"):
            full = " ".join(filter(None, [prof.get("first_name"), prof.get("last_name")]))
            found = await hunter.find_email_hunter(domain, full_name=full)
            if found and found.get("email"):
                candidates.append({
                    "email": found["email"].strip().lower(),
                    "first_name": found.get("first_name") or prof.get("first_name"),
                    "last_name": found.get("last_name") or prof.get("last_name"),
                    "title": found.get("title"),
                    "via": "propublica",
                    "generic": False,
                })

    if not candidates:
        return ContactResult(status="no_contact", domain=domain)

    # e) Choose by role priority; require deliverability.  Verify in
    #    priority order and return the first deliverable contact.
    for cand in sorted(candidates, key=_role_rank, reverse=True):
        verdict = await hunter.verify_email_hunter(cand["email"])
        if verdict.get("deliverable"):
            return ContactResult(
                status="resolved",
                domain=domain,
                email=cand["email"],
                first_name=cand.get("first_name"),
                last_name=cand.get("last_name"),
                title=cand.get("title"),
                via=cand.get("via"),
            )
    return ContactResult(status="no_contact", domain=domain)
