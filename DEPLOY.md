# Deploying mochi-carry-signal (AWS Lightsail + Caddy + DuckDNS)

This deploys the signal app as a **self-contained Docker stack on its own Lightsail
instance**, separate from the position-manager, and serves the dashboard over HTTPS
at **`https://mochi-carry-signal-prod.duckdns.org`**. It mirrors the PM's deployment
(Docker Compose + Caddy auto-TLS + optional Litestream backup).

```
internet ──HTTPS──> Caddy (:80/:443, auto Let's Encrypt)
                      └─reverse_proxy─> app (uvicorn :8100)
                                          ├─ poll Hyperliquid funding (hourly)
                                          └─ on Approve ──HTTPS X-Arb-Secret──> https://mochi-position-manager.duckdns.org/funding-arb/*
litestream (sidecar) ── continuous backup of ./data/signals.db ──> S3/R2 (optional)
```

The app is **never** exposed to the host directly — only Caddy's 80/443 are public.
The signals DB lives on `./data` on the host disk (survives restarts/redeploys).

---

## 0. Prerequisites

- An **AWS Lightsail** account.
- A **DuckDNS** account (free) — the subdomain **`mochi-carry-signal-prod`**
  (token at <https://www.duckdns.org>).
- The PM's **`FUNDING_ARB_SECRET`** value (this app must send the same one).

---

## 1. Create the Lightsail instance + static IP

1. Lightsail → **Create instance** → Linux/Unix → **OS Only → Ubuntu 22.04 LTS**.
2. Plan: the **$5/mo** (1 GB RAM) tier is plenty.
3. Name it e.g. `mochi-signal-prod`, create.
4. **Networking → Create static IP**, attach it to the instance. Note the IP
   (call it `SERVER_IP`).
5. **Networking → IPv4 Firewall** on the instance: add rules to allow
   **HTTP (TCP 80)** and **HTTPS (TCP 443)**. (SSH 22 is open by default.)

## 2. Point the DNS at the server

At <https://www.duckdns.org>, set the **`mochi-carry-signal-prod`** domain's IP to
`SERVER_IP` and **Update**. Verify from your laptop:

```bash
dig +short mochi-carry-signal-prod.duckdns.org      # must print SERVER_IP
```

> Lightsail's public IP is static once allocated, so a one-time DuckDNS update is
> enough. (Optional: add the DuckDNS cron updater on the box for peace of mind —
> see <https://www.duckdns.org/install.jsp>.)

## 3. Install Docker on the instance

SSH in (Lightsail browser SSH, or `ssh ubuntu@SERVER_IP`), then:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker      # run docker without sudo
docker compose version                               # sanity check
```

## 4. Get the code

```bash
git clone https://github.com/haufung80/mochi-carry-signal.git
cd mochi-carry-signal
```

## 5. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Set at least:

```ini
# Reach the position-manager over its public DNS:
PM_BASE_URL=https://mochi-position-manager.duckdns.org
# MUST equal the PM's FUNDING_ARB_SECRET (sent as the X-Arb-Secret header):
FUNDING_ARB_SECRET=<the-PMs-funding-arb-secret>
# Gate for approve/reject in the dashboard — set a strong random value:
APP_SECRET=<long-random-string>
# Live firing on approval (still approve-to-fire only). Use true to smoke-test.
DRY_RUN=false

# Optional: this app's OWN Telegram bot (separate from the PM's) for signal alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional: off-box DB backup (see the LITESTREAM_* block; safe to leave blank)
```

> `DATABASE_URL` is set by `docker-compose.prod.yml` to `/app/data/signals.db` —
> leave the one in `.env` alone.

The **`Caddyfile`** is already set to `mochi-carry-signal-prod.duckdns.org`; no edit
needed unless you chose a different hostname.

## 6. Launch

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Caddy will fetch the Let's Encrypt cert on first start (needs steps 1–2 done:
DNS → SERVER_IP, ports 80/443 open). Watch it:

```bash
docker compose -f docker-compose.prod.yml logs -f caddy     # cert issuance
docker compose -f docker-compose.prod.yml ps                # all "Up", app healthy
```

## 7. Verify

```bash
curl -s https://mochi-carry-signal-prod.duckdns.org/healthz      # -> ok
```

Then open **<https://mochi-carry-signal-prod.duckdns.org>** in a browser — the dashboard
(funding-history charts + signal log) should load over HTTPS. Approving a pending
signal there will POST to the PM's `/funding-arb/open` with the shared secret.

---

## Updating to a new version

```bash
cd mochi-carry-signal
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The `./data` volume (DB + Caddy certs) is preserved across rebuilds.

## Backups (Litestream → S3/R2)

Off by default (the sidecar idles). To enable, fill the `LITESTREAM_*` block in
`.env` (Cloudflare R2 free tier recommended) and restart:

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f litestream
```

Restore after a rebuild on a fresh box (run BEFORE `up`, with `.env` in place):

```bash
docker run --rm -v "$PWD/data:/data" -v "$PWD/litestream.yml:/etc/litestream.yml:ro" \
  --env-file .env litestream/litestream:0.3.13 \
  restore -if-replica-exists -config /etc/litestream.yml /data/signals.db
```

## Security notes

- Only Caddy's **80/443** are public; the app's 8100 is internal to the compose
  network. Keep the Lightsail firewall to 22/80/443.
- **`APP_SECRET`** gates approve/reject — set a strong value for a public deploy.
- **`FUNDING_ARB_SECRET`** must equal the PM's; it travels to the PM as the
  `X-Arb-Secret` header over HTTPS.
- The signal app holds **no exchange keys** — it only talks to the PM, which owns
  execution. Compromise of this box cannot place trades except via the PM's
  secret-gated funding-arb API (and only OPEN/CLOSE of the cash-and-carry combo).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Caddy can't get a cert (logs show ACME errors) | DNS not pointing to `SERVER_IP` yet, or 80/443 not open in the Lightsail firewall. Fix steps 1–2, then `docker compose ... restart caddy`. |
| `502` from the domain | App not healthy yet. `docker compose ... logs -f app`; check `.env`. |
| Dashboard loads but Approve fails | `FUNDING_ARB_SECRET` mismatch with the PM, or `PM_BASE_URL` wrong/unreachable. |
| Charts say "No funding history" | The app can't reach Hyperliquid (`api.hyperliquid.xyz`) — check outbound network; `docker compose ... logs -f app`. |
