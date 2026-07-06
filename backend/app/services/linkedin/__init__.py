"""LinkedIn provider package.

All actions go through Unipile's hosted browser API (real Chrome on
residential IPs).  The DIY Playwright / hybrid / linkedin-api HTTP
providers were removed once Unipile proved it could keep sessions alive
without our own fingerprint/proxy bookkeeping; the ABC is preserved in
case a second hosted provider ever needs to slot in alongside Unipile.
"""
from app.services.linkedin.base import (
    AccountRestricted,
    ActionResult,
    ChallengeRequired,
    InboundEvent,
    LinkedInProvider,
    ProfileRef,
)
from app.services.linkedin.unipile_impl import UnipileLinkedInProvider

_provider: LinkedInProvider | None = None


def get_provider() -> LinkedInProvider:
    """Return the env-configured LinkedIn provider singleton.

    Legacy/no-tenant-context path only — tenant work must go through
    :func:`ambient_provider` (BYOK, multi-tenancy Phase 4)."""
    global _provider
    if _provider is None:
        _provider = UnipileLinkedInProvider()
    return _provider


async def ambient_provider() -> LinkedInProvider:
    """BYOK provider for the ambient tenant.  Strict in tenant context:
    a tenant without Unipile creds gets an explicitly-empty provider
    (raises the 'DSN not set' error at the first call, the existing
    not-configured behavior) — NEVER the platform env keys.  With no
    tenant context, falls back to the env singleton."""
    from app.services.tenant_keys import ambient_creds
    from app.tenancy.context import current_tenant_id

    uc = await ambient_creds("unipile")
    if uc is not None:
        return UnipileLinkedInProvider(dsn=uc.dsn, api_key=uc.api_key)
    if current_tenant_id.get() is not None:
        return UnipileLinkedInProvider(dsn="", api_key="")
    return get_provider()


def _reset_provider_for_tests() -> None:
    """Reset the singleton.  Test-only; never call from app code."""
    global _provider
    _provider = None


__all__ = [
    "AccountRestricted",
    "ActionResult",
    "ChallengeRequired",
    "InboundEvent",
    "LinkedInProvider",
    "ProfileRef",
    "UnipileLinkedInProvider",
    "get_provider",
]
