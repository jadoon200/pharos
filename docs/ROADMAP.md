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
| M4 | Flagship model — compact GRU sequence autoencoder (torch; train/val + early stopping) vs Isolation Forest/PCA baselines; cross-region generalization | ✅ done |
| M5 | Composite ensemble — fuse detectors into per-vessel maritime-threat rollups (transparent risk + reliability) | ✅ done |
| M6 | Eval harness — gold set + calibration trap, per-type P/R, cross-region AUC, GFW cross-check → docs/EVAL.md | ✅ done |
| M7 | API — hardened read-only FastAPI + GeoJSON endpoints + `POST /score-track` | ✅ done |
| M8 | Dashboard — map-based React/Leaflet site (Maritime Picture / Incidents / Model Report), browser-verified | ✅ done |
| M9 | Deploy — `Dockerfile.web` + `render.yaml` free plan, baked demo SQLite seed, keep-alive; demo-seed script | ✅ config landed |
| M10 | **Extra** — ARGUS GEOINT bridge: `GET /geoint/evidence` exposes incidents as citable, ARGUS-`EvidenceItem`-shaped GEOINT evidence | ✅ done |

**Status:** the full system (M0–M10) is built. The whole pipeline
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

## Resolved design questions

- **NOAA slice:** LA / Long Beach, 2020-01-01 (352 vessels, 145,028 reports, 388 tracks) is the
  recorded real-data validation corpus. The raw slice stays out of Git because AIS data is large.
- **Trajectory model:** an 8-unit GRU sequence autoencoder over ordered track dynamics is the
  flagship. A 25-run capacity sweep selected it over the 64-unit version (804 vs 38,660
  parameters); Isolation Forest and linear PCA are retained as fair baselines. The operating
  comparison is recorded in [`EVAL.md`](EVAL.md).
- **Cross-region split:** train on Singapore and score US west-coast traffic. Heading-invariant
  track-shape descriptors make this a meaningful transfer test.
- **External-label window:** east Gulf, 2023-07-25 (`27.0–30.5°N, 93.0–88.0°W`). A deterministic
  label-enriched NOAA cohort yields real GFW agreement for rendezvous (4/34) and loiter (65/298).
  The GFW gap label is offshore beyond NOAA receiver coverage, so gap calibration remains open.
- **Reproducible real-data slicing:** `scripts.filter_noaa` streams national NOAA ZIPs by bbox/MMSI;
  `scripts.select_gfw_cohort` retains every matched labelled vessel plus deterministic background.
  Commands and checksums are recorded in [`EVAL.md`](EVAL.md).
- **Rendezvous scaling:** a conservative slow-motion space/time index cuts the full east-Gulf
  slice from 937,765 possible pairs to 51,570 exact candidates. The 1.39M-report detector run now
  completes in 7.12s after loading; indexed/exhaustive cohort output is exactly identical.

## Next build priorities

1. **Publish and smoke-test the public demo.** The Render blueprint is implemented and locally
   buildable; connecting it to Render, recording the URL, and verifying `/health` plus the SPA are
   deployment operations still to do.
2. **Find a receiver-compatible gap corpus.** GFW's Gulf gap events are offshore satellite labels;
   the NOAA terrestrial feed does not observe both endpoints. Add a source/window with compatible
   reception before claiming real gap calibration.
3. **Generate and commit the Linux dependency lock.** CI currently falls back to
   `requirements.txt` until the manual Lock workflow creates `requirements.lock`.
4. **Acquire real trajectory-anomaly labels.** The compact GRU wins the current multi-seed
   synthetic/cross-region comparison, but model-family promotion needs externally labelled real
   route anomalies rather than reconstruction-error anecdotes.
