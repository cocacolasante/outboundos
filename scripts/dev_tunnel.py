#!/usr/bin/env python3
"""Refresh the Unipile webhooks against a fresh ngrok tunnel.

Idempotent one-shot dev workflow:
  1. Reuse an already-running ngrok (via 127.0.0.1:4040) or start a fresh
     ``ngrok http 8000`` in the background.
  2. Read UNIPILE_DSN / UNIPILE_API_KEY / UNIPILE_WEBHOOK_SECRET from .env.
  3. List the Unipile workspace's webhooks and delete any whose
     ``request_url`` doesn't point at the current tunnel host.
  4. Create three fresh webhooks (sources: messaging, account_status, users)
     pointing at ``<tunnel>/webhooks/unipile``.
  5. Update ``WEBHOOK_BASE_URL`` in .env (and ``UNIPILE_WEBHOOK_SECRET`` if
     ``--rotate-secret``) preserving every other line.
  6. ``docker compose up -d --force-recreate backend worker beat`` so the
     containers pick up the new env values.

Run from the project root with the host Python:
    python3 scripts/dev_tunnel.py [--rotate-secret] [--dry-run] [--no-recreate]

Requires: ngrok installed + an authtoken configured (``ngrok config
add-authtoken <T>``).  Pure stdlib — no pip install needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
NGROK_ADMIN = "http://127.0.0.1:4040/api/tunnels"
NGROK_START_TIMEOUT = 15  # seconds
WEBHOOK_SOURCES = ("messaging", "account_status", "users")
DEFAULT_AUTH_HEADER = "X-Unipile-Auth"


# ---- coloured printing -----------------------------------------------------


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def info(msg: str) -> None:
    print(_c("• ", "36") + msg)


def ok(msg: str) -> None:
    print(_c("✓ ", "32") + msg)


def warn(msg: str) -> None:
    print(_c("! ", "33") + msg)


def die(msg: str, code: int = 1) -> None:
    print(_c("✗ ", "31") + msg, file=sys.stderr)
    sys.exit(code)


# ---- .env handling ---------------------------------------------------------


def parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        die(f".env not found at {path}")
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env_in_place(path: Path, updates: dict[str, str]) -> None:
    """Rewrite .env, updating only the keys in ``updates`` (preserving every
    other line — comments, blank lines, ordering)."""
    lines = path.read_text().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    pat = re.compile(r"^(\s*)([A-Z_][A-Z0-9_]*)\s*=(.*)$")
    for line in lines:
        m = pat.match(line)
        if m and m.group(2) in updates:
            key = m.group(2)
            out.append(f"{m.group(1)}{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    # Append any keys that weren't present yet.
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")


# ---- ngrok -----------------------------------------------------------------


def _ngrok_url_from_admin() -> str | None:
    try:
        with urllib.request.urlopen(NGROK_ADMIN, timeout=2) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError):
        return None
    for tunnel in data.get("tunnels") or []:
        url = tunnel.get("public_url") or ""
        if url.startswith("https://"):
            return url
    return None


def ensure_ngrok(port: int = 8000) -> str:
    """Return the https public URL of an active ngrok tunnel, starting one
    if needed."""
    existing = _ngrok_url_from_admin()
    if existing:
        ok(f"ngrok already running: {existing}")
        return existing

    if not shutil.which("ngrok"):
        die("ngrok not installed.  brew install ngrok or see https://ngrok.com.")

    info(f"starting `ngrok http {port}` in the background…")
    log_path = ROOT / ".ngrok.log"
    log = log_path.open("w")
    # Detach so the tunnel survives this script exiting.
    proc = subprocess.Popen(
        ["ngrok", "http", str(port), "--log=stdout"],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.time() + NGROK_START_TIMEOUT
    while time.time() < deadline:
        url = _ngrok_url_from_admin()
        if url:
            ok(f"ngrok up: {url}  (log at {log_path})")
            return url
        if proc.poll() is not None:
            die(
                "ngrok exited before producing a tunnel.  Check "
                f"{log_path} (common cause: missing authtoken — run "
                "`ngrok config add-authtoken <YOUR_TOKEN>`)."
            )
        time.sleep(0.4)
    die(
        f"ngrok did not produce a public URL within {NGROK_START_TIMEOUT}s.  "
        f"Check {log_path}."
    )


# ---- Unipile API -----------------------------------------------------------


class Unipile:
    def __init__(self, dsn: str, api_key: str) -> None:
        host = dsn if "://" in dsn else f"https://{dsn}"
        self.base = host.rstrip("/")
        self.api_key = api_key

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "X-API-KEY": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            die(
                f"Unipile {method} {path} → {exc.code}: "
                f"{exc.read().decode(errors='replace')[:300]}"
            )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def list_webhooks(self) -> list[dict[str, Any]]:
        data = self._req("GET", "/api/v1/webhooks")
        if isinstance(data, dict):
            return list(data.get("items") or data.get("webhooks") or [])
        if isinstance(data, list):
            return list(data)
        return []

    def delete_webhook(self, webhook_id: str) -> None:
        self._req("DELETE", f"/api/v1/webhooks/{webhook_id}")

    def create_webhook(
        self, source: str, request_url: str, secret: str,
        auth_header: str = DEFAULT_AUTH_HEADER,
    ) -> dict[str, Any]:
        return self._req("POST", "/api/v1/webhooks", body={
            "name": f"emailblaster - {source}",
            "request_url": request_url,
            "source": source,
            "headers": [
                {"key": "Content-Type", "value": "application/json"},
                {"key": auth_header, "value": secret},
            ],
        })


# ---- Brevo API -------------------------------------------------------------

BREVO_API = "https://api.brevo.com/v3"
BREVO_AUTH_HEADER = "X-Brevo-Auth"
# Events we want pushed in real time (suppression + engagement).
BREVO_EVENTS = [
    "delivered", "opened", "click", "hardBounce", "softBounce",
    "spam", "unsubscribed", "blocked", "invalid",
]


class Brevo:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{BREVO_API}{path}", data=data, method=method,
            headers={
                "api-key": self.api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 404 "document_not_found" just means no webhook exists yet.
            if exc.code == 404:
                return None
            die(f"Brevo {method} {path} → {exc.code}: "
                f"{exc.read().decode(errors='replace')[:300]}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def list_transactional(self) -> list[dict[str, Any]]:
        data = self._req("GET", "/webhooks?type=transactional")
        if isinstance(data, dict):
            return list(data.get("webhooks") or [])
        return []

    def delete(self, webhook_id: Any) -> None:
        self._req("DELETE", f"/webhooks/{webhook_id}")

    def create(self, url: str, secret: str) -> dict[str, Any]:
        return self._req("POST", "/webhooks", body={
            "type": "transactional",
            "url": url,
            "description": "Email Blaster real-time events",
            "events": BREVO_EVENTS,
            "headers": [{"key": BREVO_AUTH_HEADER, "value": secret}],
        })


def refresh_brevo_webhook(
    env: dict[str, str], tunnel_url: str, tunnel_host: str,
    *, dry_run: bool, rotate_secret: bool,
) -> dict[str, str]:
    """Best-effort: point Brevo's transactional webhook at the live tunnel.
    Returns the .env keys to update.  No-op (with a note) when BREVO_API_KEY
    isn't configured."""
    api_key = env.get("BREVO_API_KEY")
    if not api_key:
        info("BREVO_API_KEY not set — skipping Brevo webhook refresh.")
        return {}
    secret = env.get("BREVO_WEBHOOK_SECRET") or ""
    if rotate_secret or not secret:
        secret = secrets.token_hex(32)
        info(f"using a fresh BREVO_WEBHOOK_SECRET (rotate-secret={rotate_secret})")
    webhook_url = f"{tunnel_url.rstrip('/')}/webhooks/brevo"

    br = Brevo(api_key)
    existing = br.list_transactional()
    if dry_run:
        for w in existing:
            print(f"  would delete Brevo webhook id={w.get('id')} url={w.get('url')}")
        print(f"  would create Brevo webhook url={webhook_url} header={BREVO_AUTH_HEADER}")
        return {}
    for w in existing:
        wid = w.get("id")
        if wid is not None:
            info(f"deleting Brevo webhook {wid} ({(w.get('url') or '')[:60]}…)")
            br.delete(wid)
    created = br.create(webhook_url, secret)
    ok(f"created Brevo webhook id={created.get('id') if isinstance(created, dict) else '?'}")

    updates = {}
    if env.get("BREVO_WEBHOOK_SECRET", "") != secret:
        updates["BREVO_WEBHOOK_SECRET"] = secret
    return updates


# ---- main ------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rotate-secret", action="store_true",
                   help="Generate a fresh UNIPILE_WEBHOOK_SECRET (default: reuse existing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without modifying anything.")
    p.add_argument("--no-recreate", action="store_true",
                   help="Skip the docker compose force-recreate step.")
    p.add_argument("--port", type=int, default=8000,
                   help="Local port ngrok should forward (default: 8000).")
    args = p.parse_args()

    env = parse_env(ENV_PATH)
    dsn = env.get("UNIPILE_DSN")
    api_key = env.get("UNIPILE_API_KEY")
    if not dsn or not api_key:
        die("UNIPILE_DSN and UNIPILE_API_KEY must be set in .env.")
    auth_header = env.get("UNIPILE_WEBHOOK_AUTH_HEADER") or DEFAULT_AUTH_HEADER
    secret = env.get("UNIPILE_WEBHOOK_SECRET") or ""

    # 1. tunnel
    tunnel_url = ensure_ngrok(args.port)
    webhook_url = f"{tunnel_url.rstrip('/')}/webhooks/unipile"
    tunnel_host = tunnel_url.split("://", 1)[1].split("/", 1)[0]

    # 2. secret
    if args.rotate_secret or not secret:
        secret = secrets.token_hex(32)
        info(f"using a fresh UNIPILE_WEBHOOK_SECRET (rotate-secret={args.rotate_secret})")
    else:
        info("reusing existing UNIPILE_WEBHOOK_SECRET (pass --rotate-secret to change)")

    # 3. inspect existing webhooks
    up = Unipile(dsn, api_key)
    existing = up.list_webhooks()
    stale: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    for w in existing:
        rurl = (w.get("request_url") or "").lower()
        if tunnel_host.lower() in rurl:
            keep.append(w)
        else:
            stale.append(w)
    info(f"found {len(existing)} existing webhook(s); {len(stale)} stale, {len(keep)} already-current")

    # We always delete the existing webhooks (stale + already-current) and
    # recreate the three canonical ones — keeps the set clean even if the
    # dashboard was tweaked manually.
    to_delete = stale + keep

    if args.dry_run:
        warn("DRY RUN — no changes will be made.")
        for w in to_delete:
            label = "stale" if w in stale else "current"
            print(f"  would delete ({label}): {w.get('id')!s:36}  "
                  f"url={w.get('request_url')}")
        for src in WEBHOOK_SOURCES:
            print(f"  would create: source={src:14}  url={webhook_url}")
        refresh_brevo_webhook(env, tunnel_url, tunnel_host, dry_run=True, rotate_secret=args.rotate_secret)
        env_changes: list[str] = []
        if env.get("WEBHOOK_BASE_URL", "") != tunnel_url:
            env_changes.append(f"WEBHOOK_BASE_URL={tunnel_url}")
        if args.rotate_secret:
            env_changes.append("UNIPILE_WEBHOOK_SECRET=<new 64-hex>")
        if env_changes:
            print(f"  would set in .env: {', '.join(env_changes)}")
        else:
            print("  .env unchanged (tunnel + secret already match)")
        if not args.no_recreate and env_changes:
            print("  would run: docker compose up -d --force-recreate backend worker beat")
        elif not args.no_recreate:
            print("  skip recreate (.env unchanged — containers already have correct values)")
        return
    for w in to_delete:
        wid = w.get("id")
        if not wid:
            continue
        info(f"deleting webhook {wid} ({w.get('source','?')}, {w.get('request_url','?')[:60]}…)")
        up.delete_webhook(str(wid))

    # 5. create fresh
    created = []
    for src in WEBHOOK_SOURCES:
        info(f"creating webhook source={src}")
        created.append(up.create_webhook(src, webhook_url, secret, auth_header=auth_header))
    ok(f"created {len(created)} webhook(s)")

    # 5b. Brevo transactional webhook (best-effort; no-op without BREVO_API_KEY).
    brevo_updates = refresh_brevo_webhook(
        env, tunnel_url, tunnel_host, dry_run=False, rotate_secret=args.rotate_secret,
    )

    # 6. patch .env — only when something actually changed
    updates: dict[str, str] = {}
    if env.get("WEBHOOK_BASE_URL", "") != tunnel_url:
        updates["WEBHOOK_BASE_URL"] = tunnel_url
    if env.get("UNIPILE_WEBHOOK_SECRET", "") != secret:
        updates["UNIPILE_WEBHOOK_SECRET"] = secret
    updates.update(brevo_updates)
    if updates:
        write_env_in_place(ENV_PATH, updates)
        ok(f".env updated: {', '.join(updates.keys())}")
    else:
        info(".env unchanged (tunnel + secret already match)")

    # 7. force-recreate only when .env actually changed
    if not updates:
        ok("done — webhooks refreshed, containers untouched.")
        return
    if args.no_recreate:
        warn("skipping docker compose recreate (--no-recreate).  "
             "Run `docker compose up -d --force-recreate backend worker beat` "
             "manually for the changes to take effect.")
        return
    info("recreating backend/worker/beat so the new .env is picked up…")
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate",
             "backend", "worker", "beat"],
            cwd=ROOT, check=True,
        )
    except subprocess.CalledProcessError as e:
        die(f"docker compose failed (exit {e.returncode})")
    ok("done — Unipile webhooks now point at the live tunnel.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Don't leak a noisy traceback on Ctrl-C.
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        die(f"unexpected error: {type(e).__name__}: {e}")
