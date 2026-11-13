# Deploying the Nexus API on Hugging Face Spaces (free, no card)

A **Docker Space** on the free "CPU basic" tier gives 2 vCPU / 16 GB RAM, a
public HTTPS URL, env secrets, WebSockets and streaming — enough for the whole
backend including the Chromium scraper. The same `Dockerfile` is used; the
YAML header at the top of the repo's `README.md` tells Spaces which port the
app listens on (`app_port: 8000`).

What is different from the VM setup:

| | VM (`docker-compose.yml`) | Hugging Face Space |
|---|---|---|
| HTTPS | Caddy | provided by Spaces (`https://<user>-nexus-api.hf.space`) |
| Redis | compose container | none — the app falls back to in-memory rate limiting/caches automatically (or set `REDIS_URL` to a free [Upstash](https://upstash.com) database) |
| Secrets | `/opt/nexus/.env` | Space **Settings → Variables and secrets** |
| Sleep | never | after **48 h without requests**; wakes on the next request (~1 min) |
| Visibility | n/a | the Space must be **public** — a private Space's endpoint needs HF login, which the dashboard and widget can't do. Only code is visible; secrets never are. |

## 1. Account and Space (you, ~5 min)

1. https://huggingface.co/join → create a free account (email + password, no card).
2. https://huggingface.co/new-space →
   - **Space name**: `nexus-api`
   - **License**: leave empty or MIT
   - **SDK**: **Docker** → template **Blank**
   - **Hardware**: **CPU basic · 2 vCPU · 16 GB · FREE**
   - **Visibility**: **Public**
   - **Create Space**
3. Create an access token for pushing code: https://huggingface.co/settings/tokens → **New token** → type **Write** → copy it (you'll paste it once as the git password).

## 2. Push the code (one command from the laptop)

```bash
deploy/push-to-huggingface.sh <hf-username>
# e.g. deploy/push-to-huggingface.sh sahilijaz
```

Git asks for a username (your HF username) and a password (**the token**).
The Space starts building immediately; the first build takes ~5–8 minutes
(Chromium). Watch it under the Space's **Logs** tab.

The build will fail healthchecks until the secrets below exist — add them
while it builds.

## 3. Secrets (you, Space → Settings → Variables and secrets)

Add each as a **Secret** (values are hidden) unless marked *variable*:

| Name | Value |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://USER:PASSWORD@HOST/DB?ssl=require` (Neon) |
| `SECRET_KEY` | `openssl rand -hex 48` |
| `GEMINI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME` | as in your local `.env` |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | as in your local `.env` |
| `FILE_ENCRYPTION_KEY` | as in your local `.env` (must stay the same or old files can't be decrypted) |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER/GROWTH/BUSINESS` | Stripe **test** keys |
| `BREVO_API_KEY` (or the `SMTP_*` set), `EMAILS_FROM_EMAIL` | email sending |
| `GOOGLE_CLIENT_ID` | if Google sign-in is used |
| `FRONTEND_URL` *(variable)* | the deployed dashboard URL, e.g. `https://nexus.vercel.app` |
| `DEBUG` *(variable)* | `False` |
| `ANTHROPIC_API_KEY` | leave unset unless you have a working key |

Any setting from `.env.production.example` can be added the same way. After
changing secrets the Space restarts by itself.

## 4. Verify

- `https://<user>-nexus-api.hf.space/health` → `{"status":"ok", …}`
- Space **Logs** should show `Frontend-compat schema is up to date` and
  `Application startup complete`.

## 5. Point the rest at it

1. **Frontend** (Vercel/Netlify/Cloudflare Pages): `VITE_API_BASE_URL=https://<user>-nexus-api.hf.space`, `VITE_USE_MOCK_API=false`. Put the resulting dashboard URL in the Space's `FRONTEND_URL`.
2. **Stripe webhook**: `https://<user>-nexus-api.hf.space/api/billing/webhook` → copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
3. **Google OAuth**: add the dashboard URL to authorised JavaScript origins.
4. **Keep it awake for a demo**: open the `/health` URL once before presenting, or use a free pinger (e.g. cron-job.org) hitting `/health` every 30 minutes.

## Updating

Push again with the same command after merging changes to `dev`; Spaces
rebuilds and restarts. Schema changes apply at startup.

## Limits to know

- 48 h idle sleep (free tier). Background sweeps (indexing recovery,
  retention) only run while the Space is awake.
- No persistent disk (not needed — files are in Cloudinary, data in Neon).
- One container: scale is "one Space". Plenty for an FYP and early users.
