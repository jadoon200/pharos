# PHAROS

**Maritime domain awareness & GEOINT** — turns free vessel-movement data (AIS) into
source-rated, human-review **maritime incidents**: dark ships, ship-to-ship transfers,
loitering / zone incursions, AIS spoofing, and trajectory anomalies — fused into a composite
per-vessel threat picture, the way a maritime fusion cell actually works.

> **The GEOINT lane of the portfolio.** Sibling to [SENTINEL](../sentinel) (cyber threat
> intelligence) and [ARGUS](../argus) (all-source / information defence). Those fuse the
> *cyber* and *cognitive* pictures; PHAROS fuses the **spatiotemporal** one — where and when,
> on the water. Named for the lighthouse of Alexandria: a watcher over the sea.

> **Status:** the full standalone system runs end-to-end. Collection (NOAA bulk AIS loader +
> AISStream live client + Global Fishing Watch labels + a deterministic labelled synthetic
> generator), track building, the five-detector ensemble, the flagship trajectory-anomaly model,
> the composite maritime-threat rollup, the honest eval harness, a hardened read-only API, and a
> React/Leaflet map dashboard are all in place and browser-verified. Remaining polish: the free
> cloud deploy config is written (see [`docs/DEPLOY.md`](docs/DEPLOY.md)) and the optional ARGUS
> GEOINT bridge is a documented extra. Progress is tracked honestly in
> [`docs/ROADMAP.md`](docs/ROADMAP.md); every model/detector claim lands in
> [`docs/EVAL.md`](docs/EVAL.md) with the number that survives scrutiny.

## Why maritime, why Singapore

Maritime domain awareness is arguably *the* most operationally relevant intelligence problem
for Singapore: the Strait of Malacca and the Singapore Strait, the world's busiest container
port, the RSN Information Fusion Centre, and the current-salience problems of dark-fleet
sanctions evasion, illegal fishing, smuggling, and gray-zone maritime activity. PHAROS works
this domain on entirely free, open data.

## What it does

Ingest free AIS → build per-vessel tracks → run a battery of detectors, each covering a
different maritime threat, then **fuse them into a composite per-vessel maritime-threat
rollup** (which detectors agree, the zone context, a transparent risk score, and a NATO
Admiralty-style reliability grade for AIS confidence):

1. **Dark-ship / AIS-gap** — a vessel that goes silent in a sensitive zone and reappears
   displaced (the classic smuggling / sanctions-evasion signature).
2. **Ship-to-ship (STS) transfer** — two vessels co-located, slow, offshore, for a sustained
   window (dark-fleet oil transfers).
3. **Loitering / zone incursion** — dwelling in or entering a geofenced watch area.
4. **AIS spoofing / identity anomaly** — physically impossible kinematics or duplicate identity.
5. **Trajectory anomaly** — the flagship model: a **GRU sequence autoencoder** that learns
   pattern-of-life from the ordered track and flags deviations (trained the honest way — benign
   train/val split, early stopping, a recorded learning curve).

## Honest evaluation (the discipline)

Two evaluations, and [`docs/EVAL.md`](docs/EVAL.md) leads with the second:

- **Real NOAA AIS is the test that counts.** On one real day of LA/Long Beach traffic (352 vessels,
  145k reports) the detectors immediately exposed a real weakness — a congested port is full of
  anchored vessels sitting slow-and-near each other, which naive detectors call ship-to-ship
  transfers and loitering. Three domain-correct fixes (anchored-vessel exclusion, port-zone
  awareness, a partner-degree cap) cut false positives **~98% (rendezvous 2,892 → 8, total
  2,999 → 51)**. That measured before/after is the actual result. The flagship GRU, trained on the
  real pattern-of-life, surfaces the genuinely-distinctive tracks (the Catalina Island high-speed
  ferries) as the top anomalies — interpretable, real outliers.
- **The flagship model beats fair baselines — the depth is necessary.** Under the realistic
  *unsupervised* setup (train on all tracks, no labels), the GRU sequence-AE holds **~0.96 AUC**
  (within *and* cross-region) while the linear-PCA baseline **falls below chance
  (~0.27)**. The recurrent architecture that models ordered dynamics is what survives.
- **Honest about the synthetic ceiling.** The labelled offline gold set can't ship hundreds of MB
  of NOAA CSVs, so it uses a deterministic simulator (noise, benign confounders, graded anomalies).
  But self-generated anomalies are separable *by construction* — so the near-perfect detector P/R
  and high synthetic AUC are a **ceiling, not a capability claim**. The one informative synthetic
  number is the baseline gap above.
- **The AIS coverage confound is handled, not hidden.** A "dark ship" is often a receiver-coverage
  gap — handled via a displacement requirement, an AIS reliability grade (on real data a 20 h gap
  was graded **E**), a calibration trap (held 5/5), and the GFW cross-check. Incidents are
  **human-review decision support, never automated verdicts**.

## Data sources (all free)

[NOAA Marine Cadastre AIS](https://marinecadastre.gov/ais/) (bulk historical, no key — the
reproducible eval workhorse), [AISStream.io](https://aisstream.io/) (free live global
WebSocket — the demo lane, includes Malacca/Singapore), [Global Fishing Watch
API](https://globalfishingwatch.org/our-apis/) (free event labels for cross-check), and Natural
Earth / EEZ reference geometry.

## Stack

Python 3.12 (conda) · SQLAlchemy 2.0 / Alembic · PostgreSQL (SQLite for tests — no PostGIS
dependency; spatial math in pure numpy) · Prefect · httpx · scikit-learn · **torch GRU sequence
autoencoder** (flagship anomaly model; **PCA baseline**) · FastAPI · React 19 + TypeScript +
Leaflet · Docker Compose · GitHub Actions. Mirrors SENTINEL/ARGUS conventions so the three read as
one body of work. ruff + mypy (strict) + pytest gate every change.

## Quickstart

```bash
make env && conda activate pharos && make install   # one-time
make up                                             # Postgres + migrations (Docker)
make ingest FILE=data/ais/<slice>.csv REGION=us-west  # load a free NOAA AIS slice
make tracks                                         # build per-vessel voyages
make detect                                         # run the detector ensemble → incidents
make eval                                           # score detectors on the gold set → docs/EVAL.md
make eval-real FILE=data/ais/<slice>.csv REGION=us-la  # the honest test on real AIS
make api                                            # read-only API + GeoJSON on :8000
make ui                                             # React map dashboard on :5173
```

No NOAA download to try it? Seed a self-contained demo from the deterministic synthetic
generator (zones + tracks + the full detector ensemble + the anomaly model) into a SQLite file,
then serve the API + dashboard off it:

```bash
PHAROS_DATABASE_URL=sqlite:///data/demo.db python -m scripts.seed_demo
PHAROS_DATABASE_URL=sqlite:///data/demo.db make api   # then `make ui` in another shell
```

### API endpoints

`/health`, `/stats`, `/vessels` (+ `/{mmsi}/track` as GeoJSON), `/incidents` (+ `/{id}` with
evidence), `/zones` and `/tracks` (GeoJSON for the map), `/maritime-picture` (the composite
per-vessel threat rollups), `/geoint/evidence` (incidents as citable, source-rated GEOINT evidence
— the bridge below), plus one stateless inference route `POST /score-track` (scores a pasted
track's shape through the anomaly model — inspects only the supplied points, never fetches a URL,
so the API stays effectively read-only). Public-deploy hardening (CORS, per-client rate limit →
429, request-size cap → 422, bounded concurrency → 503) is tuned via `PHAROS_API_*`.

### GEOINT bridge — cyber + cognitive + geospatial

`GET /geoint/evidence` shapes each maritime incident into an evidence item whose fields match
ARGUS's `EvidenceItem` (doc_id / title / source / NATO-Admiralty reliability A–F / credibility 1–6
/ summary / resolvable url) plus geospatial extras. So the sibling **[ARGUS](../argus)** all-source
analyst — which already fuses **[SENTINEL](../sentinel)**'s cyber campaigns the same read-only way —
can cite PHAROS's maritime picture with no schema translation: one analyst reasoning across the
cyber, cognitive, *and* geospatial lanes. PHAROS only serves; it never depends on the siblings.

### Dashboard

A React 19 + TypeScript + Leaflet dashboard (own maritime-teal identity) with three views:
**Maritime Picture** (a live map of tracks, geofenced zones, and threat markers; click a threat
for the evidence chain — which detectors agree → techniques → zone → the transparent composite
risk breakdown and AIS reliability grade), **Incidents** (a filterable ranked feed), and **Model
Report** (the per-threat coverage, the honest-evaluation story, and a live "score a track" panel).

### Deploy

A single free container (`Dockerfile.web` + `render.yaml`) builds the dashboard, installs the
slim API runtime, and bakes the demo SQLite seed, serving the read-only API + the built SPA from
one service — no managed database. See [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Responsible use

Public / open-source AIS only; vessel-level (never individual persons); defensive and
analytical only. Incidents flag *patterns in public broadcast data* for human review — they are
decision support, never an automated verdict of illicit activity. See
[`docs/MODEL_CARD.md`](docs/MODEL_CARD.md).
