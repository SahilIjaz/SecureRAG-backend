# Deploying the Nexus API on Render (free tier, no card)

`render.yaml` at the repo root is a Render **Blueprint**: one free Docker web
service for the API plus a free Key Value (Redis) instance. Render builds the
`Dockerfile` with `INSTALL_CHROMIUM=false`, so the image fits the 512 MB
free instance; the website scraper is disabled (`CRAWL4AI_ENABLED=false`).

## Limits of the free tier (know these before the demo)

| Limit | Effect |
|---|---|
| 512 MB RAM, 0.1 CPU | fine for the API; no Chromium → **URL scraping is off** (uploads, FAQs, chat, widget, conflicts, audit all work) |
| **Sleeps after 15 min idle** | first request after that takes ~50 s; open `/health` before presenting, or ping it every 10 min with a free pinger (cron-job.org) |
| Background tasks pause while asleep | indexing recovery / retention sweeps run only when the service is awake |
| 750 free instance-hours per month | one always-on service fits |
| Outbound SMTP blocked | use `BREVO_API_KEY` (HTTP API) for email — already wired |

Upgrade path: the same blueprint on the **Starter** plan ($7/month, 512 MB →
can set `INSTALL_CHROMIUM=true` on 2 GB "Standard") or the VM setup in
`deploy/README.md`.

## 1. Account (you, 2 min)

https://dashboard.render.com/register → **Sign up with GitHub** (no card).
Grant Render access to the `SahilIjaz/SecureRAG-backend` repository when
asked (you can limit it to that one repo).

## 2. Create the services from the blueprint (you, 3 min)

1. Dashboard → **New +** → **Blueprint**.
2. Pick **SecureRAG-backend**; Render finds `render.yaml` → **Connect**.
3. Blueprint name: `nexus`. It lists the two services and asks for the
   secrets marked `sync: false`. Paste them from your local `.env`:
   `DATABASE_URL` (Neon, with `?ssl=require`), `GEMINI_API_KEY`,
   `PINECONE_API_KEY`, `CLOUDINARY_*` (3), `FILE_ENCRYPTION_KEY`,
   `STRIPE_*` (5, test keys), `BREVO_API_KEY`, `EMAILS_FROM_EMAIL`,
   `GOOGLE_CLIENT_ID`. For `FRONTEND_URL` enter `http://localhost:5173` for
   now and change it once the dashboard is deployed.
4. **Apply**. The first build takes ~3–5 minutes (no Chromium). The service
   URL is `https://nexus-api.onrender.com` (or `nexus-api-xxxx.onrender.com`
   if the name is taken — it's shown at the top of the service page).

## 3. Verify

- `https://<service>.onrender.com/health` → `{"status":"ok", …}`
- Service → **Logs**: `Frontend-compat schema is up to date`,
  `Application startup complete`, `Connected to Redis`.

Then send the URL to whoever is verifying; the dashboard flow can be run
against it exactly like against localhost.

## 4. Point the rest at it

1. **Frontend**: Vercel/Netlify/Cloudflare Pages, `VITE_API_BASE_URL=https://<service>.onrender.com`, `VITE_USE_MOCK_API=false`. Then set `FRONTEND_URL` on the Render service to the dashboard URL (Environment tab → Save → it redeploys).
2. **Stripe webhook**: `https://<service>.onrender.com/api/billing/webhook` → signing secret into `STRIPE_WEBHOOK_SECRET`.
3. **Google OAuth**: add the dashboard URL to authorised origins.

## Updating

Push/merge to `dev` → Render redeploys automatically (`autoDeploy: true`).
Schema changes apply at startup.
