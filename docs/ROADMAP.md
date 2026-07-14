# PHAROS roadmap

The GEOINT lane of the SENTINEL/ARGUS portfolio: maritime domain awareness on free AIS data.
Staged so each milestone lands a usable increment; the "floor to ship" is the full standalone
story (M0–M8), with deploy (M9) last and the ARGUS bridge (M10) an explicit extra.

| # | Milestone | Status |
|---|---|---|
| M0 | Scaffolding — repo skeleton, config/logging, geospatial primitives, zone registry, DB models + migration, CI, test harness | ✅ done |
| M1 | Collection — NOAA bulk AIS loader, synthetic labelled generator, AISStream live client, GFW label client, zone/reference seed, Prefect flows | ✅ done |
| M2 | Track building — per-MMSI segmentation (gap-split voyages), resampling, heading-invariant shape features, kinematics | ✅ done |
| M3 | Deterministic detectors — dark-ship/gap, STS rendezvous, loiter/incursion, spoofing → incidents | ✅ done |
| M4 | Flagship model — GRU sequence autoencoder (torch; train/val + early stopping) vs a PCA baseline; cross-region generalization | ✅ done |
| M5 | Composite ensemble — fuse detectors into per-vessel maritime-threat rollups (transparent risk + reliability) | ✅ done |
| M6 | Eval harness — gold set + calibration trap, per-type P/R, cross-region AUC, GFW cross-check → docs/EVAL.md | ✅ done |
| M7 | API — hardened read-only FastAPI + GeoJSON endpoints + `POST /score-track` | ✅ done |
| M8 | Dashboard — map-based React/Leaflet site (Maritime Picture / Incidents / Model Report), browser-verified | ✅ done |
| M9 | Deploy — `Dockerfile.web` + `render.yaml` free plan, baked demo SQLite seed, keep-alive; demo-seed script | ✅ config landed |
| M10 | **Extra** — ARGUS GEOINT bridge: `GET /geoint/evidence` exposes incidents as citable, ARGUS-`EvidenceItem`-shaped GEOINT evidence | ✅ done |

**Status:** the full system (M0–M10) is built and gating clean (79 tests). The whole pipeline
(collect → tracks → 5 detectors → flagship anomaly model → composite ensemble → honest eval) runs
end-to-end, a hardened read-only API serves it, the React/Leaflet dashboard is browser-verified
against the live API, the single-container free-deploy path is verified, and the GEOINT bridge
exposes incidents as evidence any all-source tool (the sibling ARGUS) can fuse — cyber + cognitive
+ geospatial on one analyst's desk.

**Recommended floor to ship:** exceeded — M0–M8 is the full standalone system, M9 makes it
deployable, and the M10 bridge (originally optional) is done.

## Design decisions already locked

- **Zero-cost / runs-offline.** Free sources only (NOAA bulk AIS needs no key; AISStream and GFW
  keys are optional). Mirrors SENTINEL/ARGUS.
- **No heavy geo stack.** Spatial math (haversine, point-in-polygon) is pure numpy in
  `pharos.geo`; Postgres is the prod store, SQLite runs the tests — no PostGIS dependency.
- **Honest evaluation over demos.** No detector/model claim ships without a measured number and
  recorded negatives in `docs/EVAL.md`. The AIS coverage confound is the honesty centerpiece.
- **Responsible use.** Public/open-source AIS only, vessel-level, defensive/analytical; incidents
  are human-review decision support, never automated verdicts.

## Open questions (resolve as we build)

- NOAA slice selection — which zone/day gives a rich enough mix of traffic + genuine events for a
  reproducible eval without ballooning `data/`? Fix a small cached slice and record it.
- Trajectory-anomaly model form — sequence autoencoder (reconstruction error) vs next-position
  predictor vs trajectory clustering baseline; benchmark and record the operating point.
- Cross-region split — which pair of regions makes the fairest generalization test (traffic
  regime differs enough to be a real test, not a different planet)? Record the choice.
