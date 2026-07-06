# Setup — API keys & first run

How to obtain every credential the app uses and where to put it. All values go
in `.env` (copy from [`.env.example`](.env.example)).

> After editing `.env`, changes only reach the containers on a recreate:
> ```bash
> docker compose up -d --force-recreate backend worker beat
> ```
> Verify with `docker compose exec backend printenv VAR_NAME`.

---

## 1. Required to run

The app boots without the optional keys below, but these five are needed for
core functionality (compose + send email).

### Anthropic (AI compose / research)
1. Sign up at <https://console.anthropic.com>.
2. **Settings → API Keys → Create Key**, copy it.
3. `ANTHROPIC_API_KEY=sk-ant-...`

Model + cost knobs (optional, sane defaults): `ANTHROPIC_MODEL` (compose, Sonnet),
`ANTHROPIC_RESEARCH_MODEL` (research, Haiku), `RESEARCH_WEB_SEARCH_MAX_USES`
(searches per lead — the biggest cost lever, default `1`).

### Brevo (transactional email)
1. Sign up at <https://app.brevo.com>.
2. **Senders, Domains & Dedicated IPs → Senders** — add and **verify** the
   address you'll send from (click the confirmation email).
3. **SMTP & API → API Keys → Generate a new API key**, copy it.
4. ```env
   BREVO_API_KEY=xkeysib-...
   BREVO_SENDER_EMAIL=you@yourverifieddomain.com
   BREVO_SENDER_NAME=Your Name
   ```
> Deliverability: verify your domain (SPF/DKIM) in Brevo before real sends.

### App secrets (generated locally — no signup)
```bash
# ENCRYPTION_KEY (Fernet — encrypts saved IMAP credentials)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# SECRET_KEY (signs unsubscribe links; any long random string)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
```env
ENCRYPTION_KEY=<first command output>
SECRET_KEY=<second command output>
```

---

## 2. Optional — features no-op until configured

### Hunter (email finding + verification)
1. <https://hunter.io> → **API** → copy the key.  `HUNTER_API_KEY=...`
2. Free tier caps Domain Search at 10 results (`HUNTER_DOMAIN_SEARCH_LIMIT`).
> Required for nonprofit-funding discovery to resolve contacts; otherwise
> discovered orgs are notification-only.

### Apollo (lead enrichment — "deep" research mode)
1. <https://www.apollo.io> → **Settings → Integrations → API** → create a key.
2. `APOLLO_API_KEY=...`  (people search may need a paid Apollo tier.)

### Unipile (LinkedIn outreach)
LinkedIn actions run through Unipile (hosted Chrome on residential IPs). It needs
a DSN, an API key, a public webhook tunnel, and three webhooks. **Full step-by-step
is the "Unipile setup runbook" in [`CLAUDE.md`](CLAUDE.md)** — follow it top to
bottom on a fresh box. Quick version once signed up at <https://www.unipile.com>:
```env
UNIPILE_DSN=api12.unipile.com:13443        # from the dashboard top bar
UNIPILE_API_KEY=...                        # Access Tokens → Generate
UNIPILE_WEBHOOK_SECRET=...                 # random; also set as the webhook header
WEBHOOK_BASE_URL=https://<your-tunnel>     # ngrok/cloudflared host
```
Day-to-day, `python3 scripts/dev_tunnel.py` re-points the tunnel + webhooks for you.

### Adzuna (intent-engine dev-role signals)
1. <https://developer.adzuna.com> → register an app → copy **App ID** + **App Key**.
2. ```env
   ADZUNA_APP_ID=...
   ADZUNA_APP_KEY=...
   ADZUNA_COUNTRY=us
   ```

### Reply tracking (IMAP) — no API key
Connect a mailbox in-app (**Settings → Connected inboxes**); credentials are
encrypted with `ENCRYPTION_KEY`. The poller reads replies every
`IMAP_POLL_INTERVAL_MINUTES`. For Gmail, use an **App Password**, not your login.

### No-auth feeds (nothing to configure)
USASpending and IRS BMF nonprofit-funding discovery are free public feeds —
just toggle them on in **Settings → Discovery** (or `USASPENDING_ENABLED` /
`IRS_BMF_ENABLED`).

---

## 3. First run

```bash
cp .env.example .env          # then fill in section 1 (at minimum)
docker compose up -d
docker compose exec backend alembic upgrade head   # apply DB migrations
```
App → <http://localhost:5173> · API docs → <http://localhost:8000/docs>

See [`CLAUDE.md`](CLAUDE.md) for architecture, the Unipile runbook, and
operational gotchas.
