import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings, validate_required_settings
from app.routers import (
    admin,
    agent,
    auth,
    billing,
    icp,
    intent_profiles,
    signals,
    analytics,
    campaigns,
    connected_accounts,
    leads,
    linkedin_accounts,
    preview,
    report_builder,
    reports,
    research_client,
    sequences,
    social_radar,
    settings as settings_router,
    stripe_webhooks,
    webhooks,
    crm,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="OutboundOS",
    version="0.1.0",
    docs_url="/api-docs",
    redoc_url="/api-redoc",
)


# Fail fast on missing API keys / insecure defaults.  Without this the
# app boots happily with an empty ANTHROPIC_API_KEY and only errors at
# the first compose attempt — long after the user has uploaded leads
# and started a campaign.  Running this at import time means
# ``docker compose up`` prints the problem and exits non-zero.
try:
    validate_required_settings()
except Exception as exc:  # noqa: BLE001
    logger.critical("Config validation failed: %s", exc)
    raise

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# CSRF belt-and-braces on top of SameSite=Lax session cookies: a mutating
# request that carries a browser Origin header must come from our frontend.
# Webhooks (Unipile/Brevo/Stripe) and the unsubscribe confirm-page POST are
# exempt — they are either server-to-server (no Origin) or same-origin pages
# served by this backend, and neither authenticates via the session cookie.
_CSRF_EXEMPT_PREFIXES = ("/webhooks", "/unsubscribe")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    if (
        request.method in _MUTATING_METHODS
        and not request.url.path.startswith(_CSRF_EXEMPT_PREFIXES)
    ):
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.FRONTEND_URL:
            return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emit a single info line per request: method, path, status, duration."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method, request.url.path, response.status_code, duration_ms,
        )
        return response
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "%s %s -> error after %.1fms",
            request.method, request.url.path, duration_ms,
        )
        raise


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for truly unexpected failures.

    FastAPI's built-in HTTPException + RequestValidationError handlers run
    BEFORE this one; their responses keep the standard ``{"detail": ...}``
    shape clients already depend on. This handler only fires when something
    propagates past those.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    # Starlette's ServerErrorMiddleware wraps this handler OUTSIDE CORSMiddleware,
    # so responses from here would otherwise lack CORS headers — making 500s
    # surface in the browser as opaque CORS errors instead of real failures.
    headers: dict[str, str] = {}
    origin = request.headers.get("origin")
    if origin and origin == settings.FRONTEND_URL:
        headers["access-control-allow-origin"] = origin
        headers["access-control-allow-credentials"] = "true"
        headers["vary"] = "Origin"
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "An internal error occurred"},
        headers=headers,
    )


@app.on_event("startup")
async def warn_if_rls_bypassed() -> None:
    """Loud tripwire: RLS policies present but the runtime role bypasses
    them (table owner or BYPASSRLS) — i.e. APP_DATABASE_URL isn't set to
    the non-owner ``app_user``.  Warn-not-fail so a single-tenant dev box
    still boots; production must not run this way."""
    from sqlalchemy import text

    from app.database import engine

    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT current_user, "
                "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user), "
                "EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation'), "
                "EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'campaigns' "
                "        AND tableowner = current_user)"
            ))).first()
    except Exception:  # noqa: BLE001 — a startup check must never block boot
        logger.exception("RLS bypass check failed (non-fatal)")
        return
    user, bypassrls, has_policies, is_owner = row
    if has_policies and (bypassrls or is_owner):
        logger.critical(
            "SECURITY: tenant_isolation RLS policies exist but the runtime "
            "connects as %r (table owner=%s, BYPASSRLS=%s) — row-level "
            "security is BYPASSED. Set APP_DATABASE_URL to the app_user "
            "role (scripts/bootstrap_db.sql).",
            user, is_owner, bypassrls,
        )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(billing.router)
app.include_router(stripe_webhooks.router)
app.include_router(campaigns.router)
app.include_router(leads.router)
app.include_router(preview.router)
app.include_router(analytics.router)
app.include_router(webhooks.router)
app.include_router(connected_accounts.router)
app.include_router(linkedin_accounts.router)
app.include_router(settings_router.router)
app.include_router(sequences.router)
app.include_router(research_client.router)
app.include_router(social_radar.router)
app.include_router(crm.router)
app.include_router(reports.router)
app.include_router(report_builder.router)
app.include_router(agent.router)
app.include_router(signals.router)
app.include_router(icp.router)
app.include_router(intent_profiles.router)
