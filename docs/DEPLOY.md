# Deploying PHAROS

PHAROS ships as a **single free container** that serves the read-only API and the built React
dashboard from one service, off a **baked SQLite seed** (a precomputed maritime picture over the
deterministic synthetic gold scenario). No managed database, no live fetch at request time — ideal
for a public demo. Mirrors SENTINEL/ARGUS.

This document describes the baked-demo deployment. The active Singapore pilot remains local and
outbound-only: Phase 4 publishes sanitized, derived snapshots delayed by at least 15 minutes to a
dedicated `snapshots` branch and adds explicit live/delayed/offline/demo modes to this dashboard.
No public listener or tunnel is opened on the collector laptop.

## Deploy to the cloud (free, one service)

`render.yaml` + `Dockerfile.web` are ready to go:

1. Push the repo to GitHub.
2. On [render.com](https://render.com): **New → Blueprint**, point it at the repo. Render reads
   `render.yaml`, builds `Dockerfile.web`, and deploys on the free plan with TLS + a public URL.
3. (Optional) set the repo **variable** `RENDER_URL` to the deployed base URL so
   `.github/workflows/keep-alive.yml` pings `/health` every ~10 min and reduces free-tier
   cold-starts. Scheduled workflows can be delayed, so this is not an uptime guarantee.

What the image does (`Dockerfile.web`):

- **Stage 1 (node):** `npm run build` → the static dashboard (`frontend/dist`). Same-origin API
  in the production build, so no hostname is baked in.
- **Stage 2 (python-slim):** install `requirements-api.txt` (the slim runtime — fastapi, uvicorn,
  sqlalchemy, numpy, CPU-only torch; no Prefect, no scikit-learn, no Postgres driver), install the
  package, then **bake the demo seed**: `python -m scripts.seed_demo` runs the whole pipeline
  (zones → synthetic AIS → tracks → the five detectors → the anomaly model) into
  `/app/data/demo.db`. The built SPA is copied to `frontend/dist`, which `api/app.py` mounts at `/`.
- Serves `uvicorn pharos.api.app:app` on Render's injected `$PORT`.

## App-level hardening (owned in code)

`pharos.api.limits` provides configurable CORS origins, a request-size cap (→ 422), a per-client
rate limit (→ 429), and a bounded-concurrency cap on the inference route (sheds load as 503),
tuned via `PHAROS_API_*` env vars — all off/local-safe by default. Set
`PHAROS_API_TRUST_FORWARDED_HEADER=true` behind Render's proxy so the rate-limit key is the real
client IP (spoof-safe by default otherwise). `render.yaml` sets this.

## RAM note (free tier)

Render's free plan is 512 MB. CPU-only torch plus the app is tight but fits for a demo; the
bounded-concurrency cap protects the box. If memory is a concern, the read-only endpoints
(`/maritime-picture`, `/incidents`, `/zones`, `/tracks`) serve entirely from the baked seed and
need no model — only `POST /score-track` and `POST /detect` exercise torch. A torch-free variant
could drop those two routes and shrink the image; not needed for the current demo.

## Delayed Singapore snapshots

The collector laptop never accepts inbound traffic. `make publish-snapshot` builds six fail-closed
files (`status`, `stats`, `tracks`, `incidents`, `evaluations`, and `model`) in an ignored detached
worktree, creates one orphan commit, and force-pushes only that commit to `snapshots`. Public tracks
end at least 15 minutes before generation; geometry/counts are bounded; coordinates are rounded;
the sanitizer rejects configured secrets, bearer/key-shaped text, local paths, raw AIS envelopes,
tracebacks, database URLs, and SQLite/SQLAlchemy metadata.

The frontend defaults to:

```text
https://raw.githubusercontent.com/jadoon200/pharos/snapshots/
```

Override it at build time with `VITE_SNAPSHOT_URL` for fixture/browser drills or an alternative
host. Confirm `gh repo view jadoon200/pharos --json visibility -q .visibility` returns `PUBLIC`
before relying on raw GitHub delivery. For a private repository, use a public GitHub Pages snapshot
branch or make an explicit visibility decision; never put a token in frontend code.

Every view carries a snapshot-age banner. Fresh snapshots render `live`; 10–30 minutes renders
`delayed`; older than 30 minutes renders `collector offline`; unreachable or invalid snapshots
render `demo fallback — synthetic data`. The existing baked API remains unchanged and the Pilot
view never substitutes demo metrics for missing pilot evaluation.

## Infra-level (owned at deploy time)

Reverse proxy / TLS (Render provides both), body-size limits, and RAM sizing. On a self-hosted
deploy, front the app with nginx/Caddy for TLS and a hard body cap, and size RAM to torch + the
model. For a Postgres deploy instead of the SQLite seed, set `PHAROS_DATABASE_URL` to the managed
database URL and run `alembic upgrade head` (the schema is migration-owned on Postgres; the SQLite
path self-creates on boot via `init_sqlite_schema`).
