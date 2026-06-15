# Investor Dashboard — Deploy Guide

Plotly Dash app, hosted, password-gated. Reads ONLY the curated store written by
`src/build_dashboard_dataset.py`. **PAPER ONLY** — no Kalshi auth/orders/real money; the dashboard
login is unrelated to Kalshi.

## Architecture recap
```
pipeline (local, >10x/day) ── build_dashboard_dataset.py ──▶ curated store ──▶ Dash app (hosted) ──▶ investor URL
                                  (writes 7 tables)        Postgres(cloud) / sqlite(local)        password-gated
```
Local dev uses `data/dashboard_app.db` (sqlite). Hosted uses a managed Postgres via the
`DASHBOARD_DATABASE_URL` env var — **same code, no edits**, just the env var.

## Run locally
```
pip install -r dashboard_app/requirements.txt        # already in .venv
python src/build_dashboard_dataset.py                # build the sqlite store
DASH_USERS="investor:yourpass" python dashboard_app/app.py
# open http://127.0.0.1:8050  (login: investor / yourpass)
```

## Hosted deploy — FREE stack (Render + Neon)
1. **Neon Postgres (free tier, 0.5 GB — ample):** create a project → copy the connection string
   (looks like `postgresql://user:pass@host/db`). Neon needs SSL; append `?sslmode=require` if not present.
2. **Point the pipeline at it.** On the machine running the pipeline, set
   `DASHBOARD_DATABASE_URL=<neon-url>` so `build_dashboard_dataset.py` pushes to the cloud each run.
   (One-off backfill: run it once locally with that env var set.)
3. **Render Web Service:** New → Web Service → connect repo (or deploy via Docker/zip).
   - Build command: `pip install -r dashboard_app/requirements.txt`
   - Start command: `gunicorn app:server --chdir dashboard_app --bind 0.0.0.0:$PORT`
   - Env vars: `DASHBOARD_DATABASE_URL` (Neon url), `DASH_USERS` (`investor:strongpass`).
4. Open the Render URL → log in.

### Free vs paid (the one upgrade worth procuring)
- **Free Render web tier sleeps after ~15 min idle** → a ~50s blank spinner when the investor first
  opens the link (poor first impression). Everything else (Neon, the app) is fully functional free.
- **Render Starter ($7/mo)** keeps it **always-on** (no cold start). For an investor-facing link this is
  the single worth-it upgrade. Postgres stays free on Neon. (Fly.io has a similar always-cheap option.)

### Custom domain — pros/cons
- **Host URL** (`yourapp.onrender.com`): free, instant, functional; but reads "hobby," and the URL
  changes if you switch hosts.
- **Custom domain** (`dashboard.yourproject.com`): ~$10–15/yr + 5-min DNS (CNAME to the host; TLS auto).
  Pros: credible/professional for investors, stable across host migrations, your branding. **Recommended
  for an investor-facing tool** — small cost, real credibility.

## Auth
- `DASH_USERS="user1:pass1,user2:pass2"` — **multi-account capable**; use one login for now.
- Upgrade path (when >1 investor or for better UX/security): replace BasicAuth with a login page
  (flask-login) + a hashed-password users table in the curated store. Left as a follow-up.

## Data plug-ins (already wired; populate automatically)
- **Multi-city S1 (Stage B):** as `kxhighny_multicity_s1_edge_*` artifacts gain cities, the materializer
  fills the `edge` table → the Edges + Scalability tabs update with no code change.
- **$1,000 paper run:** when the run logs daily marks into `bankroll_run` (date, bankroll,
  expected_bankroll), the Overview hero chart renders automatically. Schema is ready.

## Security / boundaries
The app and materializer read only the curated paper/research dataset. No authentication to Kalshi, no
order/account endpoints, no real money. The dashboard password gates *viewing*, nothing transactional.
