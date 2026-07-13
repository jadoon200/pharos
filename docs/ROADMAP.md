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
| M4 | Flagship model — trajectory-anomaly autoencoder (torch + MLX port, benchmark-gated); cross-region generalization (the headline) | ✅ done |
| M5 | Composite ensemble — fuse detectors into per-vessel maritime-threat rollups (transparent risk + reliability) | ✅ done |
| M6 | Eval harness — gold set + calibration trap, per-type P/R, cross-region AUC, GFW cross-check → docs/EVAL.md | ✅ done |
| M7 | API — hardened read-only FastAPI + GeoJSON endpoints + `POST /score-track` | ✅ done |
| M8 | Dashboard — map-based React/Leaflet site (Maritime Picture / Incidents / Model Report), browser-verified | ✅ done |
| M9 | Deploy — `Dockerfile.web` + `render.yaml` free plan, baked demo SQLite seed, keep-alive; demo-seed script | ✅ config landed |
| M10 | **Extra** — ARGUS GEOINT bridge (expose incidents as citable GEOINT evidence); only if time | ⬜ |

**Status:** the full standalone system (M0–M9) is built and gating clean — the whole pipeline
(collect → tracks → 5 detectors → flagship anomaly model → composite ensemble → honest eval) runs
end-to-end, a hardened read-only API serves it, and the React/Leaflet dashboard is browser-verified
against the live API. The single-container free-deploy config is written; the ARGUS GEOINT bridge
(M10) remains the one documented extra.

**Recommended floor to ship:** reached — M0–M8 is the full standalone system and M9 makes it
deployable. M10 (the ARGUS bridge) is the optional stretch.

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
