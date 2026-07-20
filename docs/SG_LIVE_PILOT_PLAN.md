# Singapore live-monitoring and evaluation pilot — amended plan (v2)

## Execution status — 2026-07-16

- **Phase 1 complete and collecting.** Pilot Day 0 began on 2026-07-16. The low-priority user
  launch agent `com.pharos.collector` is running the Class A/B AISStream collector with the
  coverage/outage ledger, SQLite WAL settings, micro-batching, downsampling, and signal-safe flush.
- **Phase 2 complete and deployed.** Dirty-MMSI track tails, affected detectors, outage-aware gaps,
  SHA-pinned frozen-GRU scoring, WAL-safe retention, and storage caps run incrementally every two
  minutes. The backend gate is green at 117 tests.
- **Scoring freeze active.** The 8-unit, 804-parameter GRU artifact SHA-256 is
  `01faa27e17dd194b8913a439034a5f71d56f48ebb291ddb073e6c9b0ee7788fb`; threshold and detector
  settings are recorded in `docs/SG_PILOT_FREEZE.md` and do not change during SG-PILOT-v0.
- **Next:** Phase 3 external/review labels and frozen evaluation, followed by Phase 4 sanitized
  delayed outbound snapshots and the dashboard's live/delayed/offline/demo modes.

This status records execution of the plan below; the original workstream requirements remain as
the acceptance contract and historical rationale.

## Feasibility verdict and amendments

The original plan (`docs/SG_LIVE_PILOT_PLAN.md`, commit `ccddac0`) is **feasible at $0** and its
honesty machinery (coverage ledger, observed-vs-calendar time, label hierarchy, frozen-model
evaluation, count-based stopping rules) is sound. Externally verified: AISStream is beta/no-SLA and
supports Class A `PositionReport` **and** Class B `StandardClassBPositionReport`, with **no explicit
redistribution terms** (so the ≥15-minute public delay stays as the conservative posture); Tailscale
Funnel is free on all plans **but the macOS App Store build cannot share ports** and requires the
standalone client, MagicDNS, tailnet policy edits, and inbound public traffic to the laptop.

Three user-approved amendments:

1. **Publish by push, not tunnel.** Workstream D is replaced: the laptop *pushes* sanitized, delayed
   JSON snapshots to a dedicated `snapshots` branch of the public `jadoon200/pharos` repo using the
   local `gh`/git CLI once authenticated (single squashed commit, force-pushed — no history bloat).
   The Render dashboard fetches `raw.githubusercontent.com` (CORS `*`). Zero inbound exposure, no new
   accounts, no beta tunnel. Funnel remains a documented upgrade path if fresher-than-CDN data is
   ever needed.
2. **Collector-first sequencing.** Collection starts as soon as Workstream A lands (days, not
   weeks). The Day-0 **freeze covers the model artifact (recorded SHA-256), thresholds, and detector
   configuration** — not the entire codebase. Non-scoring tooling (labels, review, snapshots,
   dashboard) may continue evolving during days 1–13; the pilot report records this freeze scope.
3. **ReCAAP/TSIB importer is manual-structured, not scraped.** Neither source has an API; the
   importer is a YAML → `ExternalEvent` entry tool holding minimal facts + source URLs. Review
   target confirmed at **200 blinded tracks / 50 reviewed alerts**.

Baseline gaps confirmed when this v2 plan was approved follow; the Phase 1–2 items are now
addressed, while label/evaluation and dashboard publishing remain Phase 3–4 work. The collector was
bounded-only (no reconnect, no signal handling, single end-of-window persist, Class A only);
`build_tracks` is a destructive full rebuild that also deletes incidents; no coverage/external-event/
review/evaluation tables exist; no retention or storage caps; no SQLite WAL pragmas; no composite
`(mmsi, ts)` index; the dashboard has no freshness/mode indication and no polling. Already in place
and reused: atomic GRU artifact save/load with provenance (`seq_anomaly.model_artifact_path`),
artifact-first API scorer with `model_source`, 8-hidden/804-parameter GRU, GFW vessel-aware
matching, idempotent `persist_positions` dedup, migration-parity test pattern.

## Purpose

Build a zero-recurring-cost, mobility-tolerant Singapore Strait monitoring lane that:

1. Collects live AIS while the host laptop is awake and connected.
2. Records receiver outages so laptop sleep or travel is never mistaken for vessel behaviour.
3. Builds tracks and scores them incrementally with the PHAROS detector ensemble and compact GRU.
4. Runs all training, replay, backtesting, and label matching locally.
5. Publishes only delayed, derived, read-only snapshots for the public Render dashboard.
6. Produces an honestly labelled two-week pilot report without overstating real-world precision or
   recall.

The two-week result is named **SG-PILOT-v0**. It is an engineering and evaluation pilot, not the
final real-world validation set. Promotion to **SG-REAL-v1** depends on label-count stopping
criteria rather than elapsed time alone.

## Constraints and non-goals

- Zero recurring software, data, and hosting cost; existing electricity, internet, and laptop
  storage are treated as already available.
- The collector runs on the personal M3 Pro laptop and must tolerate sleep, commuting, network
  changes, and reboots.
- **The laptop accepts no inbound public traffic.** All public data flows outward as pushed
  snapshots; there is no tunnel, port-forward, or public endpoint on the machine.
- AISStream is a free beta feed with no service-level guarantee. PHAROS must expose feed freshness
  and never imply continuous coverage when the collector was offline.
- The system provides vessel-level, defensive decision support for human review. It does not make
  automated legal, enforcement, or illicit-activity determinations.
- No API key, database credential, raw database content, mutation route, or training control is
  exposed publicly; the snapshot sanitizer is tested for this.
- Unlabelled Singapore AIS cannot by itself establish real anomaly precision or recall. Synthetic
  injections remain stress tests, not real-world ground truth.
- Repository commits remain under `@jaydenOoOo` only, with no co-author or AI-attribution trailers;
  local working notes and editor/tool configuration stay untracked.
- Every phase lands gate-green: ruff + ruff-format + mypy strict + pytest (run via the argus env
  python with `PYTHONPATH=src`), plus frontend `tsc -b` + eslint when touched.

## Target architecture

```text
AISStream WebSocket (Class A + B)
        │
        ▼
continuous local collector (micro-batch, reconnect, SIGTERM-safe)
        │                    │
        ▼                    ▼
SQLite WAL store      coverage/outage ledger
        │
        ▼
incremental tracks ──► deterministic detectors (outage-aware)
        │                    + frozen compact GRU scoring
        │
        ├──► local replay/backtests ──► external + review labels
        │                                      │
        ▼                                      ▼
sanitized delayed snapshot files ◄────── evaluation results
        │
        ▼  (gh force-push, every 2–5 min)
`snapshots` branch of github.com/jadoon200/pharos
        │
        ▼  (fetch raw.githubusercontent, CORS *)
Render dashboard (baked demo remains the fallback)
```

The dashboard must explicitly show one of `live`, `delayed`, `collector offline`, or
`demo fallback`, together with the snapshot `generated_at` timestamp. raw.githubusercontent CDN
caching (~5 min) is acceptable because public derived tracks are already delayed ≥15 minutes and
staleness is always visible via `generated_at`.

## Terminology

- **Calendar time:** wall-clock duration since the pilot started.
- **Observed time:** intervals when the collector was connected and receiving a healthy feed.
- **Coverage outage:** a known collector, laptop, network, or provider interruption.
- **Vessel gap:** a vessel-specific absence observed while the receiver feed remained healthy.
- **External label:** an event identified independently of PHAROS.
- **Review label:** a blinded human assessment of a track under a written label guide.
- **Public snapshot:** a delayed and bounded projection of derived data pushed to the `snapshots`
  branch — never the local database, never raw AIS envelopes.
- **Freeze:** the recorded SHA-256 of the GRU artifact plus thresholds and detector configuration at
  collection start; operational and label tooling is explicitly outside the freeze.

## Workstream A — continuous collection

### Collector behaviour

- Refactor `pharos.ingest.aisstream` into a long-running worker (new
  `src/pharos/collector/worker.py`) while retaining the bounded `capture()` for tests and manual
  runs. New Makefile target `collector`; `make live` keeps the bounded semantics.
- Subscribe to the existing Singapore/Malacca box (`0.9–1.5°N`, `103.3–104.2°E`,
  `settings.aisstream_bbox`).
- Extend `parse_message` to Class B `StandardClassBPositionReport`; accept `ShipStaticData` as
  optional identity enrichment (name/call sign/type onto `Vessel`) that must never block position
  processing. Update the server-side `FilterMessageTypes` accordingly.
- Store `source="aisstream"` and a stable operational region `singapore-live` (replacing the ad-hoc
  `live`).
- Buffer reports in memory ≤ `collector_batch_seconds` (default 45 s), then commit one idempotent
  micro-batch through the existing `ensure_vessels` + `persist_positions` (`_ts_key` dedup already
  makes restarts idempotent).
- Downsample at accept time: at most one stored position per vessel per
  `collector_downsample_seconds` (default 45 s), **unless** heading change ≥ 20°, speed change
  ≥ 2 kn, or nav-status change — those intermediate reports are preserved (they feed the detectors).
- Reconnect with bounded exponential backoff + jitter (1 s → `collector_backoff_max_seconds`,
  default 300 s) on socket, DNS, auth, or network failure.
- Handle `SIGTERM`/`SIGINT`: flush the current micro-batch, close the outage/run record, close the
  DB cleanly. At most one micro-batch of loss on abrupt kill.
- Structured health logs (structlog, existing `pharos.logging`); the API key and the subscription
  payload containing it are never logged.

### Laptop mobility and outage integrity

- New tables (migration `0003_collector_coverage`): `collector_runs` (id, started_at,
  last_message_at, stopped_at, stop_reason, report_count, vessel_count, status) and
  `coverage_outages` (id, opened_at, closed_at, reason, run FK). Open an outage when the feed goes
  unhealthy (no message > 90 s, or disconnect); close it only after reconnection **and** receipt of
  valid reports. On startup the worker also bridges the window since the previous run's end
  (laptop off, clean stop, crash) as a `collector offline between runs` outage, closed by the new
  run's first valid report — downtime between runs is receiver silence, never vessel silence.
- Never create a dark-ship/AIS-gap incident whose silent interval overlaps a known coverage outage
  (`detect/gaps.py` consults the ledger). Tracks crossing an outage carry reduced coverage
  reliability or are excluded from evaluation, per detector.
- Backtest denominators use observed time, not calendar time (observed hours computed from
  `collector_runs` minus outages).
- On AC power the worker keeps running with the display asleep (user sets "Prevent automatic
  sleeping on power adapter" — documented, not automated). On battery/travel the laptop sleeps
  normally; PHAROS records the outage rather than draining the battery.
- Install as a user-level `launchd` agent (`ops/com.pharos.collector.plist`): `KeepAlive` with
  restart-on-failure, `RunAtLoad`, logs to `~/Library/Logs/pharos-collector.log`. Documented
  `launchctl bootstrap/bootout` commands.

### Retention and storage

- Pilot DB is a local SQLite file in WAL mode: `make_engine` applies
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` pragmas for sqlite URLs.
- Composite index `(mmsi, ts)` on positions (same migration 0003) — required for incremental tail
  queries at live volume.
- Retain raw/downsampled live positions `retention_positions_days` (default 21); retain tracks,
  incidents, matches, reviews, model artifacts, evaluation summaries, and coverage records
  indefinitely.
- `scripts/prune.py` runs pruning only after `PRAGMA wal_checkpoint(TRUNCATE)` succeeds. Warn at
  2 GB (`storage_warn_gb`); at 5 GB (`storage_hard_gb`) stop accepting nonessential raw samples
  (downsampling floor rises; labels/evaluations are never auto-deleted).

## Workstream B — incremental detection and model operations

- The worker maintains a dirty-MMSI set per micro-batch. Every `process_interval_minutes` (1–5,
  default 2) a processing cycle (new `src/pharos/tracks/incremental.py`) rebuilds **only affected
  track tails**: for each dirty MMSI, load positions from the last completed track boundary
  (composite index makes this cheap), re-segment, upsert `Track` rows non-destructively — never the
  current `delete(Incident)` + `delete(Track)` full rebuild, which stays available as
  `make tracks` for offline work.
- Deterministic detection runs only on affected vessels; rendezvous candidates are pre-filtered to
  dirty vessels' spatiotemporal neighbourhoods (existing partner-degree cap and port-zone exclusions
  unchanged). A fixed regression fixture asserts incremental output == full recompute.
- Completed or sufficiently long windows are scored with the **persisted frozen GRU artifact**
  (`SequenceAnomalyModel.load(model_artifact_path(...))`) — no retraining on the request path.
- Keep the 8-hidden-unit, 804-parameter GRU unless a frozen external-label evaluation supports a
  change. Retraining (if any) is nightly/weekly, off-path, writes a NEW artifact with provenance,
  and does not replace the pilot's frozen artifact during the window.
- Artifact persistence (already implemented) carries training window, observed coverage, feature
  shape, normalization provenance, threshold, seed, and version; extend metadata with the artifact
  SHA-256 recorded in `docs/SG_PILOT_FREEZE.md`.
- `torch.set_num_threads(2)` (and `OMP_NUM_THREADS`) in worker/backtest entrypoints so scheduled
  work never occupies all performance cores.
- Thresholds are never tuned on the final reporting holdout; any change motivated by SG-PILOT-v0 is
  evaluated on a later frozen window.

## Workstream C — Singapore labels and backtesting

### Label hierarchy

| Tier | Source | Use | Limitation |
|---|---|---|---|
| external incident | ReCAAP Singapore Strait reports/map | known incident retrieval and ranking | an incident does not always cause an anomalous trajectory |
| external incident | Singapore TSIB marine reports | verified casualty/collision/allision cases | high quality but sparse and often delayed |
| silver event | GFW encounter, loiter, gap, and port-visit events | vessel/type/time/place agreement | algorithmic, incomplete, and delayed |
| trajectory review | blinded human track labels | direct route/kinematic anomaly assessment | reviewer effort and subjectivity |
| controlled stress | anomalies injected into real Singapore background | sensitivity and regression testing | not real precision or prevalence |

### Label data model

Migration `0004_labels_eval`, new package `src/pharos/labels/`:

- `external_events`: source, source id/URL, event type, vessel identifiers, start/end, location,
  source confidence, retrieval time, attribution.
- `event_track_matches`: event FK, track FK, identifier-match flag, temporal distance, spatial
  distance, matching-rule version, match status (`matched` / `unmatched` / `ambiguous`).
- `track_reviews`: track FK, anonymized review order, label, subtype, confidence, reason, reviewer
  id, review time, adjudication status.
- `evaluation_runs`: frozen data/model/label versions (incl. artifact SHA), sampling design,
  metrics JSON, confidence intervals, exclusions, recorded negatives, created_at.

### Importers and matching

- **GFW** (`labels/external.py`): reuse `ingest/gfw.py` fetch; now *persist* events into
  `external_events` (previously fetch-only). Encounter/loiter/gap + port visits; required GFW
  attribution stored with each event.
- **ReCAAP/TSIB** (no APIs): a YAML entry format (one file per incident: source URL, date/time,
  position, vessel identifiers if published, incident type, retrieval date) imported by
  `labels/external.py`; store structured facts and links only — no long copyrighted text.
- **Matching** (`labels/match.py`): identifier match where MMSI/IMO available; else time
  (≤ 24 h) + space (≤ 25 km) against tracks in observed coverage; every external event during
  observed coverage ends `matched` or explicitly `unmatched`; rule version recorded.

### Review protocol

- Freeze the model artifact and threshold before the evaluation window starts (Phase 1 freeze).
- `labels/review.py` CLI builds a blinded queue mixing all external-event tracks, PHAROS alerts,
  near-threshold tracks, random apparently-normal tracks, and likely AIS-quality artifacts;
  model score, detector output, and sampling stratum are hidden until the label is submitted
  (each review ~30–60 s: renders the track polyline + kinematics only).
- Labels: `normal`, `route_or_kinematic_anomaly`, `rendezvous_or_loiter`,
  `verified_external_incident`, `ais_data_artifact`, `uncertain`. `uncertain` is reviewed
  separately, never silently counted as negative.
- Single-reviewer reality: blind re-review of 10–20 % after ≥ 3 days, agreement reported as a
  stated limitation.

### Metrics

Reported per label source, by `eval/pilot.py` into an `evaluation_runs` row:

- Precision among reviewed PHAROS alerts; recall against matched external events.
- Weighted ROC-AUC/PR-AUC for the sampled reviewed benchmark when sampling weights are valid.
- Detector/subtype results only where positive counts suffice; otherwise `not estimable`.
- Confidence intervals (Wilson), sample counts, exclusions, unresolved matches, unknown labels.
- Alert rate per 100 observed vessel-hours; FP concentration by reliability grade, port/anchorage
  zone, vessel type, coverage state; score drift by day and vessel type.
- Throughput, end-to-end latency, uptime, reconnect count, observed-vs-calendar time.

### Two-week stopping rules

SG-PILOT-v0 targets: ≥ 200 blinded reviewed tracks; ≥ 50 reviewed PHAROS alerts; blind re-review of
10–20 %; all external events during observed coverage imported and matched-or-marked; every result
labelled `preliminary` with denominator and CI. Promotion to SG-REAL-v1 requires ≥ 30 independent
positives overall and 20–30 per published-recall detector; if two weeks is insufficient, collection
continues without changing the frozen claim.

## Workstream D — public snapshot publication (replaces the tunneled API)

New `src/pharos/publish/snapshot.py` + `scripts/publish_snapshot.sh`; no public server runs on the
laptop.

Snapshot files (JSON, one per former endpoint), written to a working dir and force-pushed as a
single squashed commit to the `snapshots` branch every `publish_interval_minutes` (default 3):

- `status.json` — mode (`live`/`delayed`/`collector offline`), last source report time, last
  processing time, observed uptime, current outage state, `generated_at`.
- `stats.json` — bounded counts (vessels, accepted reports, tracks, incidents, per-detector totals).
- `tracks.json` — recent derived tracks, **delayed ≥ 15 min**, geometry simplified
  (Douglas-Peucker), response-size capped.
- `incidents.json` — bounded human-review candidates with reliability grades and neutral language
  (`candidate` / `corroborated` / `reviewed` / `unresolved`).
- `evaluations.json` — latest frozen pilot/backtest result, sample sizes, CIs, versions,
  limitations.
- `model.json` — artifact source, parameter count, training window, threshold, provenance, SHA.

Requirements:

- Push-only: no `/detect`, ingestion, query, file, or admin capability is reachable from outside —
  there is nothing listening.
- The sanitizer strips and a test asserts absence of: API keys, tokens, raw AIS envelopes, local
  filesystem paths, internal error text, database metadata, and `.env` content.
- Public derived tracks delayed ≥ 15 minutes (AISStream publishes no redistribution terms; this is
  the conservative posture). Status/stats fields are aggregate and may be fresher.
- Every file carries `generated_at`, a source/freshness/reliability statement, and the human-review
  disclaimer; GFW attribution accompanies anything GFW-derived.
- Publishing uses the locally authenticated `gh`/git CLI; failures are logged and retried next
  cycle (public site falls back gracefully; the collector never depends on publishing).

## Workstream E — Render showcase

- Keep the baked demo dataset; the public site stays useful when the laptop is asleep.
- `frontend/src/api.ts` gains a snapshot client with base
  `https://raw.githubusercontent.com/jadoon200/pharos/snapshots/` and TanStack
  `refetchInterval: 60_000`; no message-level streaming.
- Mode banner on every live view, driven by `status.json.generated_at`: `live` (< 10 min),
  `delayed` (10–30 min), `collector offline — showing last snapshot` (> 30 min), or
  `demo fallback — synthetic data` (snapshot fetch fails). Never silently substitute data.
- New Singapore Pilot section (`views/Pilot.tsx` or a Maritime Picture panel): observed hours,
  reports, vessels, completed tracks, alert rates, coverage outages, model version/SHA, label
  counts, preliminary metrics with CIs, recorded limitations, attributions.
- Incident language stays neutral; Render free-tier cold starts are acceptable.

## Resource budget for the M3 Pro laptop

| Resource | Target | Protective action |
|---|---:|---|
| steady CPU | below 5% average | incremental work, `torch.set_num_threads(2)`, scheduled backtests |
| steady PHAROS memory | below 1.5 GB | micro-batches, bounded caches, separate offline backtests |
| backtest peak memory | below 4 GB | stream/chunk large corpora and fail before memory pressure |
| live-data disk warning | 2 GB | prune eligible raw reports after WAL checkpoint |
| live-data hard limit | 5 GB | stop nonessential sampling and preserve derived/label data |
| snapshot publish | every 2–5 min | single squashed force-push; capped file sizes |
| dashboard polling | 60 s | raw.githubusercontent CDN absorbs it; `generated_at` shows staleness |
| unflushed data | ≤ 60 s | micro-batch commit and graceful shutdown |

The laptop may sleep normally during travel. The design prioritizes battery and data honesty over
pretending to provide uninterrupted coverage.

## Delivery sequence (collector-first)

### Phase 1 — build the collector, then start collecting (complete)

1. Migration 0003: `collector_runs` + `coverage_outages` + composite `(mmsi, ts)` index; WAL
   pragmas in `make_engine`; config keys (`collector_*`, `retention_*`, `storage_*`,
   `process_interval_minutes`, `publish_interval_minutes`); regression tests + migration-parity.
2. Class B + static-enrichment parsing; continuous worker with micro-batching, downsampling,
   reconnect/backoff, SIGTERM flush, health logs; `launchd` plist + docs; bounded `capture()`
   retained.
3. Sleep/wake + Wi-Fi-loss drill (scripted): proves reconnect, a recorded outage, idempotent dedup.
4. **Freeze**: record artifact SHA-256 + thresholds + detector config in `docs/SG_PILOT_FREEZE.md`;
   start the collector under launchd. **Pilot Day 0 begins here.**

### Phase 2 — incremental processing + outage integrity (complete)

5. `tracks/incremental.py` dirty-set tail rebuilds; outage-aware gap suppression; frozen-artifact
   scoring in the cycle; incremental==full regression fixture; retention/pruning + storage caps.
6. Days 1–3 operational tuning only (batch/retention/downsample) — never model thresholds.

### Phase 3 — labels, review, evaluation (pilot days 3–13, off the scoring path)

7. Migration 0004 + `labels/` package: GFW persistence, ReCAAP/TSIB YAML importer, matching rules,
   blinded review CLI, `eval/pilot.py` evaluation runner.
8. Import delayed labels as they publish; run the blinded queue toward 200/50; daily health/drift
   summary script.

### Phase 4 — publish + showcase (parallel with Phase 3)

9. `publish/snapshot.py` + sanitizer tests + `snapshots` branch push; frontend snapshot client,
   mode banner, Pilot section; browser-verify all four modes; one real end-to-end publish smoke.

### Phase 5 — pilot close-out

- **Day 14:** freeze the data snapshot; run the pre-registered evaluation; write SG-PILOT-v0 into
  `docs/EVAL.md` (preliminary, with denominators and CIs) and `evaluations.json`.
- **After Day 14:** continue collection until SG-REAL-v1 stopping counts are met. Amend
  the status across `docs/SG_LIVE_PILOT_PLAN.md`, `docs/ROADMAP.md`, and the runbook; delegate the
  documentation pass to a lightweight subagent per user preference.

## Acceptance criteria

### Collector and mobility

- Reconnects automatically after sleep, Wi-Fi changes, and temporary provider failure.
- Persists no more than one micro-batch of potential loss on an abrupt stop.
- Records all known offline intervals and reports observed time separately.
- Produces no normal-confidence dark-ship call whose gap crosses a known collector outage.
- Deduplication remains idempotent across restarts (existing `_ts_key` path, tested).

### Detection and evaluation

- Incremental results match a full recomputation on a fixed regression fixture.
- Batch-trained and API-scored anomaly models use the same persisted artifact (already tested);
  the pilot's frozen artifact SHA appears in every evaluation row.
- Pilot evaluation is reproducible from frozen model, data, label, and sampling versions.
- Metrics separate official, reviewed, silver, and synthetic/stress labels.
- Insufficient-positive cases say `not estimable`; they are never rendered as zero or omitted.

### Public showcase and security

- No process on the laptop accepts public inbound traffic; publishing is outbound-only.
- Snapshot sanitizer test proves no key, token, raw envelope, private path, internal error, or DB
  metadata appears in any published file.
- Laptop sleep produces an honest offline/fallback display (stale `generated_at`), never an
  apparent live view; the four dashboard modes are browser-verified.
- Render cannot invoke collection, detection, training, or mutation (nothing to invoke).
- Source attribution (incl. GFW) and human-review limitations are visible.
- Backend gate, frontend lint/build, and the sleep/wake/network-loss drill pass.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| laptop sleeps during collection | record outage, reconnect on wake, exclude outage from vessel-gap inference |
| AISStream interruption or sparse coverage | status/freshness metrics, no SLA claim, reliability grading |
| too few real positive events in two weeks | publish a pilot only; continue to event-count stopping rules |
| false negatives hidden in unlabelled traffic | independent external-event ingestion plus blinded random review |
| reviewer bias (single reviewer) | hide scores/strata, delayed blind re-review of 10–20%, report agreement |
| disk growth | downsampling, rolling retention, warn/hard limits, WAL checkpoints |
| snapshot leaks local capability or secrets | outbound-only publishing; sanitizer + tests; bounded file sizes |
| raw.githubusercontent CDN staleness (~5 min) | acceptable under the 15-min delay; `generated_at` always displayed; Funnel documented as upgrade path |
| local machine unavailable during a demo | timestamped last snapshot + explicit baked-demo fallback |
| model is tuned to the pilot | freeze artifact SHA before collection; evaluate changes only on a later holdout |
| source restrictions or attribution missed | delayed derived output, minimal facts, source links, GFW attribution, provider confirmation |

## Cost

| Component | Pilot choice | Recurring cost |
|---|---|---:|
| live AIS | AISStream beta API | $0 |
| compute/model/backtesting | personal M3 Pro laptop | $0 incremental |
| operational database | local SQLite WAL | $0 |
| external event labels | ReCAAP, TSIB, GFW | $0, subject to source terms/attribution |
| public snapshot hosting | `snapshots` branch + raw.githubusercontent | $0 |
| dashboard | Render free web service | $0, with cold starts |

## Source references

- [AISStream API documentation](https://aisstream.io/documentation) — verified: beta/no-SLA, Class
  A + B message types, no explicit redistribution terms
- [Render free-tier limits](https://render.com/docs/free)
- [ReCAAP reports](https://www.recaap.org/reports) and
  [incident map](https://portal.recaap.org/OpenMap) — no API; manual structured import
- [Singapore TSIB marine investigation reports](https://www.mot.gov.sg/what-we-do/transport-investigations/marine/marine-reports/)
- [Global Fishing Watch Events API](https://globalfishingwatch.org/our-apis/documentation)
- PHAROS evaluation methodology: `docs/EVAL.md` · model limitations: `docs/MODEL_CARD.md`
- Superseded: Tailscale Funnel (macOS App Store build cannot share ports; replaced by push
  snapshots — kept as a documented upgrade path)
