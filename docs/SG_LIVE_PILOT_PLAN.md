# Singapore live-monitoring and evaluation pilot

## Purpose

Build a zero-recurring-cost, mobility-tolerant Singapore Strait monitoring lane that:

1. Collects live AIS while the host laptop is awake and connected.
2. Records receiver outages so laptop sleep or travel is never mistaken for vessel behaviour.
3. Builds tracks and scores them incrementally with the PHAROS detector ensemble and compact GRU.
4. Runs all training, replay, backtesting, and label matching locally.
5. Exposes only delayed, derived, read-only results to the public Render dashboard.
6. Produces an honestly labelled two-week pilot report without overstating real-world precision or
   recall.

The two-week result will be named **SG-PILOT-v0**. It is an engineering and evaluation pilot, not
the final real-world validation set. Promotion to **SG-REAL-v1** depends on label-count stopping
criteria rather than elapsed time alone.

## Constraints and non-goals

- Zero recurring software, data, and hosting cost; existing electricity, internet, and laptop
  storage are treated as already available.
- The collector runs on the personal M3 Pro laptop and must tolerate sleep, commuting, network
  changes, and reboots.
- AISStream is a free beta feed with no service-level guarantee. PHAROS must expose feed freshness
  and never imply continuous coverage when the collector was offline.
- The system provides vessel-level, defensive decision support for human review. It does not make
  automated legal, enforcement, or illicit-activity determinations.
- No API key, database credential, raw database connection, mutation route, or training control is
  exposed publicly.
- Unlabelled Singapore AIS cannot by itself establish real anomaly precision or recall. Synthetic
  injections remain stress tests, not real-world ground truth.
- Repository commits remain under the configured project-owner identity only, with no co-author or
  AI-attribution trailers.

## Target architecture

```text
AISStream WebSocket
        │
        ▼
continuous local collector ──► coverage/outage ledger
        │
        ▼
SQLite WAL store ──► incremental tracks ──► deterministic detectors
        │                                      + compact GRU scoring
        │
        ├──► local replay/backtests ──► external + review labels
        │                                      │
        ▼                                      ▼
sanitized delayed public snapshot ◄──── evaluation results
        │
        ▼
Tailscale Funnel HTTPS ──► Render dashboard
```

The existing baked Render demo remains the fallback. The dashboard must explicitly show one of
`live`, `delayed`, `collector offline`, or `demo fallback`, together with the last successful data
timestamp.

## Terminology

- **Calendar time:** wall-clock duration since the pilot started.
- **Observed time:** intervals when the collector was connected and receiving a healthy feed.
- **Coverage outage:** a known collector, laptop, network, or provider interruption.
- **Vessel gap:** a vessel-specific absence observed while the receiver feed remained healthy.
- **External label:** an event identified independently of PHAROS.
- **Review label:** a blinded human assessment of a track under a written label guide.
- **Public snapshot:** a delayed and bounded projection of derived data, never the local database.

## Workstream A — continuous collection

### Collector behaviour

- Refactor the bounded `pharos.ingest.aisstream.capture` loop into a long-running worker while
  retaining the bounded mode for tests and manual captures.
- Subscribe to the existing Singapore/Malacca box (`0.9–1.5°N`, `103.3–104.2°E`).
- Parse Class A and supported Class B position reports. Static/voyage messages may enrich vessel
  identity but must not block position processing.
- Store the source as `aisstream` and use a stable operational region such as `singapore-live`.
- Buffer reports in memory for no more than 30–60 seconds, then commit them as one idempotent
  micro-batch.
- Downsample storage to at most one accepted position per vessel per 30–60 seconds unless a sharp
  turn, material speed change, or detector-relevant event justifies preserving an intermediate
  report.
- Reconnect with bounded exponential backoff and jitter after socket, DNS, authentication, or
  network failures.
- Handle `SIGTERM`/`SIGINT` by flushing the current micro-batch and closing the database cleanly.
- Emit structured health logs without logging the API key or subscription payload containing it.

### Laptop mobility and outage integrity

- Add a collector-run/coverage record containing start, last-message, stop, stop reason, report
  count, vessel count, and connection status.
- Open a coverage-outage interval when the feed becomes unhealthy; close it only after successful
  reconnection and receipt of valid reports.
- Never create a dark-ship or AIS-gap incident across a known coverage outage.
- Tracks crossing an outage must carry lower coverage reliability or be excluded from evaluation,
  depending on the detector.
- Backtest denominators use observed time, not calendar time.
- On AC power the worker may keep running with the display asleep. On battery or during travel the
  laptop may sleep normally; PHAROS records the outage rather than draining the battery.
- Install the worker as a user-level macOS `launchd` service with restart-on-failure and start after
  login/network recovery.

### Retention and storage

- Use SQLite in WAL mode for the pilot to avoid Docker Desktop and Postgres idle overhead.
- Retain raw/downsampled live positions for 14–30 days.
- Retain tracks, incidents, external-label matches, review labels, model artifacts, evaluation
  summaries, and coverage records longer.
- Run pruning only after a successful database checkpoint.
- Warn at 2 GB of pilot storage and stop accepting nonessential raw samples at a 5 GB hard limit;
  never delete labels or evaluation summaries automatically.

## Workstream B — incremental detection and model operations

- Maintain a set of MMSIs changed since the last processing cycle.
- Every 1–5 minutes, rebuild only affected track tails rather than the full history.
- Run deterministic detection only on affected vessels and their indexed rendezvous candidates.
- Score completed or sufficiently long track windows with the persisted GRU artifact.
- Keep the current 8-hidden-unit, 804-parameter GRU unless a frozen external-label evaluation
  supports a model change.
- Do not retrain on every message. Retrain nightly or weekly, off the request path.
- Persist the model artifact atomically with its training window, observed coverage, feature shape,
  normalization provenance, threshold, seed, and code/model version.
- Limit PyTorch and numerical-library worker threads so a scheduled backtest cannot occupy every
  performance core.
- Never tune thresholds on the same final holdout used to report performance. Any change triggered
  by SG-PILOT-v0 is evaluated on a later frozen window.

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

Add explicit provenance rather than embedding labels in detector evidence:

- `ExternalEvent`: source, source identifier/URL, event type, vessel identifiers, start/end,
  location, source confidence, retrieval time, and attribution.
- `EventTrackMatch`: event, track, identifier match, temporal distance, spatial distance, matching
  rule version, and match status.
- `TrackReview`: track, anonymized review order, label, subtype, confidence, reason, reviewer ID,
  review time, and adjudication status.
- `EvaluationRun`: frozen data/model/label versions, sampling design, metrics, confidence intervals,
  exclusions, recorded negatives, and creation time.

### Review protocol

- Freeze the model artifact and threshold before the evaluation window starts.
- Create a blinded queue mixing all external events, PHAROS alerts, near-threshold tracks, random
  apparently normal tracks, and likely AIS-quality artifacts.
- Hide model score, detector output, and sampling stratum until the reviewer submits a label.
- Use the labels: `normal`, `route_or_kinematic_anomaly`, `rendezvous_or_loiter`,
  `verified_external_incident`, `ais_data_artifact`, and `uncertain`.
- Review `uncertain` cases separately; do not silently count them as negatives.
- Prefer two independent reviewers with adjudication. If only one reviewer is available, blindly
  re-review 10–20% after a delay and report that limitation.
- Store structured facts and source links. Do not reproduce long copyrighted report text.

### Metrics

Report each label source separately:

- Precision among reviewed PHAROS alerts.
- Recall against independently collected and successfully matched external events.
- Weighted ROC-AUC/PR-AUC for the sampled reviewed benchmark when sampling weights are valid.
- Detector- and anomaly-subtype results only when their positive counts are sufficient.
- Confidence intervals, sample counts, exclusions, unresolved identity matches, and unknown labels.
- Alert rate per 100 observed vessel-hours.
- False-positive concentration by AIS reliability, anchorage/port zone, vessel type, and coverage
  state.
- Score-distribution drift by day and vessel type.
- Processing throughput, end-to-end latency, uptime, reconnects, and observed-versus-calendar time.

### Two-week stopping rules

SG-PILOT-v0 targets:

- At least 200 blinded reviewed tracks.
- At least 50 reviewed PHAROS alerts.
- A blind re-review of 10–20% of tracks if a second reviewer is unavailable.
- All external events occurring during observed coverage imported and matched or explicitly marked
  unmatched.
- Every result labelled `preliminary` and accompanied by its denominator and confidence interval.

Real precision/recall is not promoted as SG-REAL-v1 until there are at least 30 independently
identified positive events or adjudicated trajectory anomalies overall, and at least 20–30
positives for any individual detector whose recall is published. If the two-week window does not
meet those counts, collection continues without changing the frozen evaluation claim.

## Workstream D — restricted public API

Expose a separate public projection, not the internal application surface:

- `GET /public/status` — status, last source report, last processing time, observed uptime, current
  outage state, snapshot age, and mode.
- `GET /public/stats` — bounded counts of vessels, accepted reports, tracks, incidents, and detector
  totals.
- `GET /public/tracks` — simplified recent derived tracks with response and time-window limits.
- `GET /public/incidents` — bounded human-review candidates and reliability grades.
- `GET /public/evaluations/latest` — frozen pilot/backtest result, sample sizes, confidence
  intervals, model/data versions, and limitations.
- `GET /public/model` — artifact source, model size, training window, threshold, and provenance.

Public API requirements:

- GET-only; no `/detect`, training, ingestion, arbitrary query, database, file, or administrative
  route.
- Exact CORS allow-list for the Render origin.
- Rate limits, pagination, geometry simplification, response-size caps, and short-lived caching.
- No API keys, raw AIS envelopes, private filesystem paths, internal errors, or database metadata.
- Delay public derived tracks by at least 15 minutes unless AISStream explicitly confirms that
  immediate derived public redistribution is permitted.
- Include a clear source/freshness/reliability statement and human-review disclaimer.
- Add the required GFW attribution wherever GFW-derived results appear and cite other source data
  appropriately.

Use Tailscale Funnel to publish only this restricted local service over HTTPS. Funnel is available
on all Tailscale plans and supplies a `*.ts.net` hostname, but it is beta and bandwidth-limited.
The API therefore returns compact snapshots rather than a raw stream.

## Workstream E — Render showcase

- Keep the existing baked demo dataset so the public site remains useful when the laptop is asleep.
- Poll the public live API approximately every 30–60 seconds; do not attempt message-level
  streaming through Render.
- Show source mode and freshness prominently on every live-data view.
- Fall back without ambiguity: `collector offline — showing last snapshot` or
  `demo fallback — synthetic data`, never silently substitute data.
- Add a Singapore Pilot section displaying observed hours, reports, vessels, completed tracks,
  alert rates, coverage outages, model version, label counts, preliminary metrics, and recorded
  limitations.
- Keep incident language neutral: `candidate`, `corroborated`, `reviewed`, or `unresolved`, never
  `guilty`, `illegal`, or equivalent conclusions.
- Render free-tier cold starts are acceptable for the showcase; the local collector does not depend
  on Render availability.

## Resource budget for the M3 Pro laptop

| Resource | Target | Protective action |
|---|---:|---|
| steady CPU | below 5% average | incremental work, bounded threads, scheduled backtests |
| steady PHAROS memory | below 1.5 GB | micro-batches, bounded caches, separate offline backtests |
| backtest peak memory | below 4 GB | stream/chunk large corpora and fail before memory pressure |
| live-data disk warning | 2 GB | prune eligible raw reports after checkpoint |
| live-data hard limit | 5 GB | stop nonessential sampling and preserve derived/label data |
| public polling | 30–60 seconds | cache compact responses and cap geometry |
| unflushed data | no more than 60 seconds | micro-batch commit and graceful shutdown |

The laptop may sleep normally during travel. The design prioritizes battery and data honesty over
pretending to provide uninterrupted coverage.

## Delivery sequence

### Build and local validation

1. Add coverage/outage schema and regression tests.
2. Refactor the live worker for micro-batching, supported Class A/B parsing, reconnects, and clean
   shutdown.
3. Add incremental track/detector orchestration and retention controls.
4. Add model scheduling/provenance and coverage-aware evaluation exclusions.
5. Add external-label/review/evaluation schema, importers, matching, and blinded review workflow.
6. Add restricted public endpoints and tests proving mutation routes and secrets are absent.
7. Add dashboard live/offline/demo states and Singapore Pilot results.
8. Configure `launchd`, Tailscale Funnel, exact CORS, and Render integration.
9. Run the complete backend and frontend gates plus a sleep/wake/network-loss drill.

### Pilot execution

- **Day 0:** freeze code/model/config, record versions, start the collector, and verify public
  freshness/status.
- **Days 1–3:** observe ingress rate, validate outage accounting, and tune only operational batch or
  retention settings—not model thresholds.
- **Days 4–13:** collect, process, import delayed labels, review the blinded queue, and run daily
  health/drift summaries.
- **Day 14:** freeze the data snapshot and run the pre-registered pilot evaluation.
- **After Day 14:** publish SG-PILOT-v0; continue collection until SG-REAL-v1 label-count stopping
  rules are satisfied.

## Acceptance criteria

### Collector and mobility

- Reconnects automatically after sleep, Wi-Fi changes, and temporary provider failure.
- Persists no more than one micro-batch of potential loss on an abrupt stop.
- Records all known offline intervals and reports observed time separately.
- Produces no normal-confidence dark-ship call whose gap crosses a known collector outage.
- Deduplication remains idempotent across restarts.

### Detection and evaluation

- Incremental results match a full recomputation on a fixed regression fixture.
- Batch-trained and API-scored anomaly models use the same persisted artifact.
- Pilot evaluation is reproducible from frozen model, data, label, and sampling versions.
- Metrics separate official, reviewed, silver, and synthetic/stress labels.
- Insufficient-positive cases say `not estimable`; they are not rendered as zero or omitted.

### Public showcase and security

- Public routes are bounded, read-only, rate-limited, and contain no secret or internal path.
- Laptop sleep produces an honest offline/fallback display rather than an apparent live view.
- Render cannot invoke collection, detection, model training, or database mutation.
- Source attribution and human-review limitations are visible.
- Backend lint, formatting, strict typing, tests, frontend lint/build, and a browser smoke test pass.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| laptop sleeps during collection | record outage, reconnect on wake, exclude outage from vessel-gap inference |
| AISStream interruption or sparse coverage | status/freshness metrics, no SLA claim, reliability grading |
| too few real positive events in two weeks | publish a pilot only and continue to event-count stopping rules |
| false negatives hidden in unlabelled traffic | independent external-event ingestion plus blinded random review |
| reviewer bias | hide scores/strata, second review or delayed blind re-review, adjudication |
| disk growth | downsampling, rolling retention, warning/hard limits, WAL checkpoints |
| public endpoint exposes local capability | separate GET-only projection, response caps, exact CORS, tunnel only that service |
| local machine unavailable during a demo | timestamped last snapshot and explicit baked-demo fallback |
| model is tuned to the pilot | freeze before collection; evaluate changes only on a later holdout |
| source restrictions or attribution missed | delayed derived output, minimal facts, source links, required attribution, provider confirmation |

## Cost

| Component | Pilot choice | Recurring cost |
|---|---|---:|
| live AIS | AISStream beta API | $0 |
| compute/model/backtesting | personal M3 Pro laptop | $0 incremental service fee |
| operational database | local SQLite WAL | $0 |
| external event labels | ReCAAP, TSIB, GFW | $0, subject to source terms/attribution |
| public local HTTPS | Tailscale Funnel Personal/non-commercial | $0 |
| dashboard | Render free web service | $0, with cold starts/ephemeral limits |

## Source references

- [AISStream API documentation](https://aisstream.io/documentation)
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)
- [Render free-tier limits](https://render.com/docs/free)
- [ReCAAP reports](https://www.recaap.org/reports) and
  [incident map](https://portal.recaap.org/OpenMap)
- [Singapore TSIB marine investigation reports](https://www.mot.gov.sg/what-we-do/transport-investigations/marine/marine-reports/)
- [Global Fishing Watch Events API](https://globalfishingwatch.org/our-apis/documentation)
- PHAROS evaluation methodology: [`EVAL.md`](EVAL.md)
- PHAROS model limitations: [`MODEL_CARD.md`](MODEL_CARD.md)
