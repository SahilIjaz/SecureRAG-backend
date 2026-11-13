# Deploying the Nexus API on a free Oracle Cloud VM

Target: one Ubuntu VM running Docker Compose — **Caddy** (HTTPS) → **API** (this repo) + **Redis**.
Postgres (Neon), Pinecone, Cloudinary, Gemini and Stripe stay as external services.
Everything below also works on any other Ubuntu VM (DigitalOcean, Hetzner, Azure).

## 0. Files in this repo

| File | Purpose |
|---|---|
| `Dockerfile` | API image (Python 3.12 + Chromium for the scraper), non-root, healthcheck |
| `docker-compose.yml` | caddy + api + redis, volumes, memory limit, log rotation |
| `deploy/Caddyfile` | automatic Let's Encrypt HTTPS, streaming/WebSocket-safe proxy |
| `.env.production.example` | every setting with production guidance → copy to `.env` on the server |
| `deploy/setup-server.sh` | one-time VM prep: Docker, firewall 80/443, `/opt/nexus` |
| `deploy/push-from-laptop.sh` | rsync the repo to the VM and run the deploy (no GitHub creds on the server) |
| `deploy/deploy.sh` | runs on the VM: build, start, wait for healthy, print URL |

## 1. Create the Oracle Cloud account (you, ~10 min)

1. https://www.oracle.com/cloud/free/ → **Start for free**. Use the card you have; it is verified, not charged.
2. **Home region matters** and cannot be changed later. Pick one geographically close with A1 capacity (for Pakistan: *UK South (London)*, *Germany Central (Frankfurt)* or *India West (Mumbai)* are common picks).
3. If sign-up says "an error occurred" or rejects the card, wait a few hours and retry — this is common and usually succeeds on the second or third try.

## 2. Create the VM (you, ~5 min)

Console → **Compute → Instances → Create instance**

- Name: `nexus-api`
- Image: **Ubuntu 24.04** (or 22.04) — *Canonical Ubuntu*, aarch64 build
- Shape: **Ampere → VM.Standard.A1.Flex**, **4 OCPU / 24 GB** (shows "Always Free-eligible")
- Networking: create a new VCN with the defaults, **Assign a public IPv4 address = yes**
- SSH keys: **Generate a key pair for me → Save private key** (e.g. `~/.ssh/oracle_nexus.key`), then `chmod 600 ~/.ssh/oracle_nexus.key`
- Boot volume: default (up to 200 GB total is free)

If you see **"Out of capacity"**: retry every few hours or try a different availability domain; it clears within a day for most people. A 1 GB AMD micro instance also works for everything except the scraper.

Note the **Public IP** on the instance page.

## 3. Open ports 80 and 443 (you, ~2 min)

Instance page → **Primary VNIC → Subnet → Default Security List → Add Ingress Rules**

| Source CIDR | Protocol | Dest. port |
|---|---|---|
| 0.0.0.0/0 | TCP | 80 |
| 0.0.0.0/0 | TCP | 443 |
| 0.0.0.0/0 | UDP | 443 |

(`setup-server.sh` opens the same ports in the VM's own firewall.)

## 4. Hostname for HTTPS (you, ~3 min)

Caddy needs a DNS name that resolves to the VM's public IP:

- **Own domain**: add an **A record** `api` → `<Public IP>`.
- **No domain**: https://www.duckdns.org (free, sign in with GitHub/Google) → create `nexus-api` → set its IP to the VM's → hostname `nexus-api.duckdns.org`.

## 5. Prepare the server (one command from your laptop)

```bash
ssh -i ~/.ssh/oracle_nexus.key ubuntu@<Public IP> 'bash -s' < deploy/setup-server.sh
```

## 6. Deploy

```bash
deploy/push-from-laptop.sh <Public IP> ubuntu ~/.ssh/oracle_nexus.key
```

The first run stops and tells you to fill in `/opt/nexus/.env`:

```bash
ssh -i ~/.ssh/oracle_nexus.key ubuntu@<Public IP>
nano /opt/nexus/.env      # DOMAIN, DATABASE_URL, SECRET_KEY, keys… (see the comments in the file)
```

Then run the same `push-from-laptop.sh` command again. It builds the image (≈5 min the first time), starts the stack, waits for `/health`, and prints `https://<DOMAIN>`.

## 7. After the first deploy

1. **Frontend**: deploy `Nexus-frontend` to Vercel/Netlify/Cloudflare Pages with `VITE_API_BASE_URL=https://<DOMAIN>` and `VITE_USE_MOCK_API=false`; put that dashboard URL in the server's `FRONTEND_URL` and redeploy.
2. **Stripe webhook**: Dashboard → Developers → Webhooks → add `https://<DOMAIN>/api/billing/webhook`, copy the signing secret into `STRIPE_WEBHOOK_SECRET`, redeploy.
3. **Google sign-in**: add `https://<dashboard URL>` to the OAuth client's authorised origins.
4. **Widget**: in the dashboard's Deploy tab, allowed domains must include the sites that embed the widget.

## Day-to-day

```bash
deploy/push-from-laptop.sh <IP> ubuntu ~/.ssh/oracle_nexus.key   # redeploy after a change
ssh … 'cd /opt/nexus && docker compose logs -f api'                # logs
ssh … 'cd /opt/nexus && docker compose ps'                         # status
ssh … 'cd /opt/nexus && docker compose restart api'                # restart
ssh … 'cd /opt/nexus && docker compose exec api python -m pytest tests -q'   # smoke tests on the server
```

Schema changes apply themselves at startup (`ensure_frontend_schema`), so a redeploy is the whole migration story.

## Notes

- One uvicorn worker per container on purpose: background sweeps run in-process. Scale by running a second VM behind the same DNS later, not by adding workers.
- Neon's free tier suspends after 5 min idle; the first request after a pause takes ~1 s. Acceptable for a demo; a free cron ping (e.g. cron-job.org hitting `/health` every 4 min) hides it.
- Backups: Neon keeps point-in-time history; `redis_data` holds only caches/rate limits and can be lost; `caddy_data` holds the certificate (recreated automatically).
