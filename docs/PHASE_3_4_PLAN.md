# Phase 3 + Phase 4 — detailed build plan and outline

**Scope:** the two remaining build phases of the amended v2 Singapore live pilot
(`docs/SG_LIVE_PILOT_PLAN.md`). Phase 3 delivers labels, blinded review, and the frozen
evaluation machinery; Phase 4 delivers sanitized delayed snapshot publication and the public
dashboard's four honest display modes. Both run **concurrently with collection** and are
explicitly **outside the SG-PILOT-v0 freeze** (artifact SHA
`01faa27e17dd194b8913a439034a5f71d56f48ebb291ddb073e6c9b0ee7788fb`, thresholds, detector config —
none of those change here).

**Pilot clock context (2026-07-20 = pilot day 4):** Phase 3 is scheduled for pilot days 3–13, so
it is due now. Label accumulation is time-gated (GFW publishes with multi-day delay; ReCAAP
publishes as incidents occur), which makes the importers the critical-path item: the sooner they
exist, the sooner labels accrue instead of piling into backlog. Phase 4 has no data dependency at
all — every part of it can be built and verified against the existing live DB (~82k positions,
~1.7k vessels, ~345 incidents) and the baked demo. Neither phase needs the AISStream feed to be
up.

**Branch/PR strategy:** `feat/sg-pilot-phase3` and `feat/sg-pilot-phase4` as separate branches
and PRs (they touch disjoint code and Phase 4 can merge while Phase 3 review labels are still
accruing). Every merge lands gate-green: ruff + ruff-format + mypy strict + pytest via the argus
env python with `PYTHONPATH=src`, plus `tsc -b` + eslint when the frontend is touched. Commits
under `@jaydenOoOo` only; no attribution trailers.

---

## Phase 3 — labels, review, evaluation

### Objectives

1. Persist independent **external labels** (ReCAAP, TSIB, GFW) with source attribution and
   retrieval provenance.
2. Match every external event that falls inside **observed coverage** to a track — or record it
   explicitly as `unmatched`/`ambiguous`. No silent drops.
3. Run a **blinded human review** queue toward the SG-PILOT-v0 stopping targets: ≥ 200 reviewed
   tracks, ≥ 50 reviewed PHAROS alerts, 10–20 % blind re-review after ≥ 3 days.
4. Produce a **reproducible, pre-registered evaluation** (`eval/pilot.py`) keyed to the frozen
   artifact SHA, with Wilson CIs, explicit denominators, `not estimable` where counts are too
   small, and recorded negatives.
5. A **daily health/drift summary** so operational problems (like the processing-starvation bug
   found on day 4) surface within a day, not at close-out.

### Non-goals / freeze discipline

- No change to the GRU artifact, its threshold, or any detector setting. If evaluation suggests a
  change, it is written down and applied only to a *later* frozen window (SG-REAL-v1 candidate).
- No pandas/shapely/heavy deps; matching math uses `pharos.geo`; YAML parsing uses PyYAML (add to
  `requirements.txt` + `pyproject.toml` + refreeze lock — it is pure-Python, zero-cost).
- No scraping. ReCAAP/TSIB entry is manual-structured YAML with source URLs; stored facts are
  minimal (no long copyrighted prose).
- Nothing in Phase 3 runs on the collector's scoring path. All of it is offline tooling against
  the same SQLite DB (WAL means readers never block the collector's writers; long label
  operations must still avoid long write transactions — same per-phase-commit discipline as
  `process_dirty_vessels`).

### 3.1 Data model — migration `0004_labels_eval`

New ORM classes in `src/pharos/db/models.py` (keeping the single-module convention), mirrored
exactly in `migrations/versions/0004_labels_eval.py`; the existing migration↔model parity test
must pass, and every table uses `JsonType` for JSON columns so the SQLite test path stays clean.

**`external_events`**
| column | type | notes |
|---|---|---|
| `event_id` | String PK | deterministic: `"{source}:{source_ref}"` (idempotent re-import) |
| `source` | String, indexed | `recaap` / `tsib` / `gfw-encounter` / `gfw-loiter` / `gfw-gap` / `gfw-port-visit` |
| `source_ref` | String | GFW event id, ReCAAP report number, TSIB report id |
| `source_url` | Text | attribution link (required) |
| `event_type` | String | normalized: `rendezvous` / `loiter` / `gap` / `port-visit` / `incident-robbery` / `incident-collision` / … |
| `mmsi` | String, nullable, indexed | when published |
| `imo` | String, nullable | when published |
| `vessel_name` | String, nullable | as published, advisory |
| `ts_start` / `ts_end` | DateTime(timezone=True) | UTC; `ts_end` nullable |
| `lat` / `lon` | Float, nullable | point or centroid |
| `source_confidence` | String | `official` (ReCAAP/TSIB) vs `silver` (GFW algorithmic) |
| `retrieved_at` | DateTime | when we imported it (label-delay accounting) |
| `attribution` | Text | required verbatim credit line (GFW requires it) |
| `raw` | JsonType, nullable | bounded structured extras — never long prose |

**`event_track_matches`**
| column | type | notes |
|---|---|---|
| `match_id` | String PK | `"{event_id}:{track_id}"`, or `"{event_id}:∅"` for explicit unmatched |
| `event_id` | FK → external_events, indexed | |
| `track_id` | FK → tracks, nullable | NULL for `unmatched` |
| `identifier_match` | Boolean | MMSI/IMO agreed |
| `temporal_distance_s` | Float, nullable | |
| `spatial_distance_km` | Float, nullable | |
| `rule_version` | String | e.g. `match-v1` — recorded per row |
| `status` | String | `matched` / `unmatched` / `ambiguous` |
| `in_observed_coverage` | Boolean | event window intersects observed (non-outage) time |
| `created_at` | DateTime | UTC default (`_now`) |

**`track_reviews`**
| column | type | notes |
|---|---|---|
| `review_id` | String PK | `"{queue_version}:{order:04d}"` |
| `track_id` | FK → tracks, indexed | |
| `queue_order` | Integer | anonymized presentation order |
| `stratum` | String | sampling stratum — **hidden from the reviewer until submission**, stored for weighting |
| `label` | String, nullable | `normal` / `route_or_kinematic_anomaly` / `rendezvous_or_loiter` / `verified_external_incident` / `ais_data_artifact` / `uncertain` |
| `subtype` | String, nullable | free-form short tag |
| `confidence` | String, nullable | `low` / `medium` / `high` |
| `reason` | Text, nullable | one-line reviewer note |
| `reviewer_id` | String | `primary` for the pilot; re-reviews use `primary-rereview` |
| `reviewed_at` | DateTime, nullable | NULL = still queued |
| `adjudication` | String, nullable | for re-review disagreements: `agree` / `disagree` / `resolved-<label>` |

**`evaluation_runs`**
| column | type | notes |
|---|---|---|
| `run_id` | String PK | `"SG-PILOT-v0:{created_at:%Y%m%dT%H%M%S}"` |
| `artifact_sha256` | String | must equal the frozen SHA; asserted at runtime |
| `data_snapshot` | JsonType | position/track/incident counts, min/max ts, observed hours |
| `label_versions` | JsonType | per-source event counts + retrieval cutoffs, review queue version, match rule version |
| `sampling_design` | JsonType | strata definitions + weights |
| `metrics` | JsonType | the full metrics block (see 3.5) |
| `exclusions` | JsonType | what was excluded and why (outage-crossing tracks etc.) |
| `negatives` | JsonType | recorded negative results, verbatim-honest |
| `created_at` | DateTime | UTC |

Indexes: `external_events(source)`, `external_events(mmsi)`, `event_track_matches(event_id)`,
`track_reviews(track_id)`. Migration parity test extends automatically; add explicit round-trip
tests on SQLite for each model.

### 3.2 Package layout — `src/pharos/labels/`

```
src/pharos/labels/
├── __init__.py
├── external.py    # GFW persistence + ReCAAP/TSIB YAML importer
├── match.py       # matching rules v1 (identifier → time+space fallback)
├── review.py      # blinded review queue builder + CLI + re-review
└── queue.py       # (optional split) stratified sampling helpers if review.py grows
src/pharos/eval/pilot.py   # evaluation runner → evaluation_runs row + evaluations.json input
scripts/pilot_health.py    # daily health/drift summary
data/labels/recaap/*.yaml  # manual entries (gitignored with the rest of data/)
data/labels/tsib/*.yaml
```

### 3.3 Importers

**GFW (`labels/external.py`, function `import_gfw_events`)**
- Reuse `pharos.ingest.gfw.fetch_events`/`parse_events` (already handles v3 dataset aliases,
  pagination, missing-token skip, tenacity retry, respx-tested). Extend `GfwEvent`/`parse_events`
  to surface the GFW event `id` and `port_visit` type (currently mapped types are
  rendezvous/loiter/gap only) — additive, keeping `eval/gfw_check.py` untouched.
- Query window: pilot start (`PHAROS_PILOT_START_AT`, 2026-07-16T02:47:51Z) → now, bbox = the
  collector's Singapore box (0.9–1.5°N, 103.3–104.2°E) taken from `settings.aisstream_bbox` so
  the label boundary equals the collection boundary exactly.
- Upsert by deterministic `event_id` (`gfw-encounter:{gfw_id}` …) via `session.merge` —
  re-imports are idempotent; `retrieved_at` refreshes on change.
- Store the required GFW attribution string on every row.
- Cadence: manual `make labels-import` daily (documented in the runbook); **not** wired into the
  collector — labels are off the scoring path and their network calls must never compete with
  the feed. GFW publishes events with days of delay: the importer is *expected* to return few or
  zero rows early in the pilot, and that emptiness is recorded (count logged, zero is a valid
  outcome, never an error).

**ReCAAP / TSIB (YAML → `external_events`)**
- One file per incident under `data/labels/{recaap,tsib}/`. Format (validated by a pydantic
  model, hard errors on unknown fields):

```yaml
# data/labels/recaap/2026-07-18-tanker-boarding.yaml
source: recaap                # or tsib
source_ref: "SS-2026-041"     # ReCAAP report number / TSIB report id
source_url: "https://www.recaap.org/..."
event_type: incident-robbery  # incident-robbery | incident-collision | incident-other
occurred_start: 2026-07-18T19:40:00Z
occurred_end: null
lat: 1.174
lon: 103.867
mmsi: null                    # only if published by the source
imo: "9876543"                # only if published
vessel_name: "EXAMPLE STAR"   # as published
source_confidence: official
retrieved: 2026-07-20
notes: "boarded at anchor; minimal facts only — no report prose"
```

- Importer `import_yaml_events(directory)` walks both directories, validates, upserts by
  `"{source}:{source_ref}"`. Storage rule enforced by a test: `notes` capped (≤ 300 chars) so no
  copyrighted report text can be persisted.
- Operational duty (user, ~5 min/day): check the ReCAAP weekly/incident map and TSIB reports
  page; transcribe any Singapore Strait incident inside the pilot bbox/window into a YAML file.
  The runbook gets a checklist section for this.

### 3.4 Matching — `labels/match.py`, rule version `match-v1`

Deterministic, order-independent, fully recorded:

1. **Coverage scoping.** Compute observed coverage as `collector_runs` intervals minus
   `coverage_outages` (reuse/extract the interval helper; today's outage math lives in
   `detect/gaps.py` and `worker._start_run` — factor a shared
   `pharos.labels.coverage.observed_intervals(session)` used by matching, evaluation, and the
   health script alike). An event is `in_observed_coverage` when
   `[ts_start, ts_end or ts_start+6h]` intersects any observed interval.
2. **Identifier rule.** If the event has an MMSI (or an IMO we can map through `Vessel` statics):
   match to that vessel's tracks overlapping the event window ± 24 h. `identifier_match=True`,
   spatial/temporal distances recorded for reporting.
3. **Time+space fallback.** No identifier → candidate tracks are those with any resampled point
   within **25 km** and **± 24 h** of the event point/window (haversine via `pharos.geo`,
   vectorized over `Track.sequence`-adjacent stored endpoints + positions; use the `(mmsi, ts)`
   index and bound the scan to the event window ± 24 h).
4. **Statuses.** Exactly one nearest candidate → `matched`. Multiple candidates within thresholds
   whose best two are within 2× of each other → `ambiguous` (all candidate rows written, one per
   track, all `ambiguous`). No candidate → one explicit `unmatched` row (track NULL).
5. Every event with `in_observed_coverage=True` **must** end with ≥ 1 match row — asserted by
   `make eval-pilot` preflight; events outside observed coverage get a single row with
   `in_observed_coverage=False` and status `unmatched` (excluded from recall denominators, and
   *that exclusion is itself reported*).
6. Thresholds (`24 h`, `25 km`, ambiguity ratio `2.0`) live in `config.py`
   (`label_match_max_hours`, `label_match_max_km`, `label_match_ambiguity_ratio`) so the rule
   version string is meaningful; changing any of them bumps `rule_version` (`match-v2`, …) and
   rematches everything (idempotent PK includes nothing rule-dependent; rows are replaced for the
   new version — keep one active rule version per evaluation run).

### 3.5 Blinded review — `labels/review.py`

**Queue construction (`build_review_queue`, versioned `queue-v1`):**
- Strata over pilot-window tracks (region `singapore-live`, `pilot_start_at` onward):
  1. `external-event` — every track matched (or ambiguous) to an external event;
  2. `pharos-alert` — tracks carrying any incident from the ensemble (all detectors), sampled
     toward the ≥ 50 reviewed-alerts target, highest composite first;
  3. `near-threshold` — anomaly reconstruction error within ± 20 % of the frozen threshold, no
     incident;
  4. `random-normal` — uniform sample of remaining tracks;
  5. `ais-artifact-candidate` — tracks with spoof incidents or implied-speed outliers.
- Deterministic seed recorded in `sampling_design`; per-stratum inclusion probabilities stored so
  weighted metrics are computable; total queue ≥ 220 (attrition margin over the 200 target).
- Rows inserted as unlabelled `track_reviews` with a shuffled `queue_order` (seeded shuffle).

**Blinding rules (tested):**
- The CLI shows **only**: an anonymized review number, the track polyline, per-step kinematics
  (speed/heading/turn profile over time), duration, and rough length. It must not show: MMSI,
  vessel identity, detector output, anomaly score, stratum, zone names, or absolute coordinates
  (positions are re-based to the track's start so shape/kinematics — the things the model sees —
  are preserved without revealing "this is inside the port"). A unit test asserts the rendered
  payload contains none of the hidden fields.
- Rendering: the CLI writes a small self-contained SVG/HTML file per track to a temp dir
  (polyline + speed sparkline, pure string templating — no new deps, no tiles, no network) and
  opens it (`open` on macOS); label entry is a terminal prompt. ~30–60 s per review.
- On submission the CLI reveals nothing; scores/strata join the label only inside `eval/pilot.py`.

**Flow:** `make review` → next unlabelled item by `queue_order` → render → prompt for
label/subtype/confidence/reason → write `track_reviews` row with `reviewed_at`. `--rereview`
builds the 10–20 % re-review sample (only items reviewed ≥ 3 days ago, seeded selection,
`reviewer_id=primary-rereview`), and `--adjudicate` walks disagreements. Agreement rate lands in
the evaluation's limitations block. `uncertain` labels are queryable separately and never counted
as negatives.

### 3.6 Evaluation runner — `src/pharos/eval/pilot.py`

`make eval-pilot` (and Phase 5 close-out) produces one `evaluation_runs` row:

- **Preflight:** frozen artifact exists and SHA matches `PHAROS_ANOMALY_MODEL_SHA256`; every
  in-coverage external event has a match row; review counts reported against the 200/50 targets
  (short counts do not fail the run — they are printed and recorded as `preliminary`).
- **Metrics (per label source, never pooled silently):**
  - Precision among reviewed PHAROS alerts (reviewed-alert stratum; Wilson 95 % CI).
  - Recall against matched external events, split `official` (ReCAAP/TSIB) vs `silver` (GFW) —
    and labelled as **agreement** for GFW, mirroring the existing EVAL.md discipline.
  - Weighted ROC-AUC/PR-AUC over the reviewed benchmark using inverse inclusion probabilities,
    only when every stratum has valid weights; otherwise `not estimable` with the reason.
  - Per-detector/subtype breakdowns only where positives ≥ 5; below that, `not estimable` —
    never zero, never omitted.
  - Alert rate per 100 observed vessel-hours (observed time from the shared coverage helper —
    calendar time is also printed for contrast, honesty centerpiece).
  - FP concentration by reliability grade, zone kind (port/anchorage vs open strait), vessel
    type, and coverage state; score drift by day and vessel type.
  - Ops block: throughput, uptime, reconnect count, outage count/duration, observed-vs-calendar.
- Output: DB row + a rendered `docs/EVAL.md` **SG-PILOT (AUTO-EVAL-PILOT)** block appended via
  the same marker mechanism `eval/run.py` uses, plus a JSON export that Phase 4's
  `evaluations.json` snapshot reads verbatim (single source of truth).
- Reproducibility test: two runs over the same frozen inputs produce identical `metrics`.

### 3.7 Daily health/drift — `scripts/pilot_health.py`

One command (`make pilot-health`) printing and appending to `data/pilot-health.log`: last source
report age, open outage (id/reason/age), outages opened in 24 h, reports/vessels per observed
hour, dirty-backlog size, DB/WAL size vs the 2/5 GB thresholds, incremental cycle duration
(parsed from the collector log), incident counts by detector over 24 h, score p50/p95 drift vs
the pilot-to-date baseline, GFW/ReCAAP label counts. Pure read-only; would have caught the
day-1–4 disconnect-per-cycle pattern immediately.

### 3.8 Config, Makefile, tests, docs

- `config.py`: `label_match_max_hours=24.0`, `label_match_max_km=25.0`,
  `label_match_ambiguity_ratio=2.0`, `review_queue_min=220`, `review_rereview_fraction=0.15`,
  `labels_dir=Path("data/labels")`.
- Make targets: `labels-import` (GFW + YAML), `labels-match`, `review`, `review-rereview`,
  `eval-pilot`, `pilot-health`.
- Tests (all on in-memory SQLite; respx for GFW): migration parity (automatic); GFW import
  idempotency + attribution present; YAML round-trip + unknown-field rejection + notes-length
  cap; matcher determinism + every-event-terminates rule + observed-coverage exclusion; queue
  strata coverage + seeded reproducibility + blinding assertion; Wilson CI values against known
  cases; `not estimable` path; evaluation reproducibility; coverage-interval helper against the
  worker's ledger semantics.
- Docs: runbook gains the daily ReCAAP/TSIB checklist + `make` cadence table; EVAL.md gains the
  pilot block; ROADMAP status flips on merge.

### 3.9 Phase 3 sequencing (target: pilot days 4–7 for machinery)

| Step | Deliverable | Depends on |
|---|---|---|
| 3a | Migration 0004 + models + parity/round-trip tests | — |
| 3b | Coverage-interval helper (shared) + tests | 3a |
| 3c | GFW persistence importer + `GfwEvent.id`/port-visit extension | 3a |
| 3d | ReCAAP/TSIB YAML schema + importer + first real backfill entries (days 0–4) | 3a |
| 3e | `match.py` v1 + preflight rule | 3b–3d |
| 3f | Review queue + blinded CLI + re-review | 3a, 3e |
| 3g | `eval/pilot.py` + EVAL.md block + reproducibility test | 3b, 3e, 3f |
| 3h | `pilot_health.py` + runbook/docs refresh; PR + merge | all |

Steps 3c/3d are worth landing first even if 3e–3g trail by days — retrieval provenance
(`retrieved_at`) starts accruing the moment the importers exist. Daily label import + review
sessions then run through day 13 while Phase 4 proceeds in parallel.

### 3.10 Phase 3 risks

| Risk | Mitigation |
|---|---|
| GFW publishes too late for the day-14 report | expected; recall vs GFW marked preliminary/agreement-only; SG-REAL-v1 stopping rules absorb the wait |
| zero ReCAAP incidents in-window | record the negative honestly; recall `not estimable`; precision/review lanes unaffected |
| single-reviewer bias | blinding + hidden strata + 10–20 % delayed re-review + reported agreement |
| review fatigue (200 × ~45 s ≈ 2.5 h total) | spread ~20/day across days 5–13; CLI optimized for speed |
| long label transactions stall the collector | read-mostly + short final commits (per-phase commit discipline, proven in PR #3) |
| YAML entries drift from source | `source_url` + `retrieved` mandatory; spot-check during re-review window |

---

## Phase 4 — publish + showcase

### Objectives

1. Outbound-only publication: sanitized, delayed, bounded JSON snapshots force-pushed to the
   dedicated `snapshots` branch; **nothing on the laptop listens**.
2. The Render dashboard gains a snapshot client and an explicit four-mode banner — `live`,
   `delayed`, `collector offline`, `demo fallback` — never silently substituting data.
3. A Singapore Pilot section presenting observed-time-honest operational and (once Phase 3
   produces them) evaluation numbers.
4. Sanitizer with tests proving no secret/path/envelope/error/DB-metadata leakage.

### 4.1 Snapshot generator — `src/pharos/publish/snapshot.py`

`build_snapshots(session, settings, out_dir)` writes six files; every file carries
`generated_at` (UTC ISO), a `source` block (AISStream + GFW attribution where applicable), a
`freshness` statement, and the human-review disclaimer ("candidates for human review, never
automated verdicts").

- **`status.json`** — `mode` (`live` / `delayed` / `collector offline`, computed from last
  source report age at generation: < 10 min / 10–30 min / > 30 min), `last_source_report_at`,
  `last_processing_at`, `observed_hours` vs `calendar_hours` (reuses the Phase 3 coverage
  helper — if Phase 4 lands first, this helper is built here and Phase 3 adopts it),
  `current_outage` (reason category + age, no internal error text), `run_count`,
  `frozen_artifact_sha256`.
- **`stats.json`** — bounded counts: vessels, accepted reports, tracks, incidents by detector,
  observed hours, alert rate per 100 observed vessel-hours.
- **`tracks.json`** — GeoJSON FeatureCollection of recent completed tracks with
  `end_ts ≤ now − 15 min` (**the delay floor is enforced here, tested**), geometry simplified
  with a new pure-numpy Douglas-Peucker in `pharos.geo` (`simplify_polyline(lat, lon,
  tolerance_km)`, unit-tested against known shapes), capped: ≤ 200 tracks, ≤ 150 points each,
  file ≤ 1.5 MB (hard-fail test).
- **`incidents.json`** — ≤ 200 most recent/highest-composite incidents: neutral fields only
  (detector, type, score, reliability grade, ts, rounded location, status
  `candidate`/`corroborated`/`reviewed`/`unresolved` — `corroborated` when an
  `event_track_matches` row links it once Phase 3 lands, `reviewed` when a `track_reviews` label
  exists), plus evidence *summaries* (duration/displacement numbers — never raw envelopes).
- **`evaluations.json`** — verbatim latest `evaluation_runs` export (Phase 3's single source of
  truth): metrics with CIs and denominators, sample counts, `not estimable` entries preserved,
  limitations text, label/rule/queue versions, artifact SHA. Until the first pilot run exists it
  publishes the existing recorded results (real-data Gulf validation summary) with an explicit
  `pilot_status: "collecting — no pilot evaluation yet"`.
- **`model.json`** — artifact source (`frozen-artifact`), SHA-256, parameter count (804), hidden
  size, threshold, training-window description, normalization provenance, freeze date, link to
  the model card.

### 4.2 Sanitizer — same module, `sanitize(payload) -> payload`

Applied to every file before write; a dedicated test suite feeds adversarial inputs and asserts
the published bytes never contain: the AISStream key or GFW token (actual values pulled from
settings and searched for), any `sk-`/`Bearer`-shaped string, `.env` content, absolute local
paths (`/Users/`, the repo path), raw AIS message envelopes (`MessageType`, `MetaData` keys),
Python tracebacks/exception text, SQLAlchemy/SQLite metadata (table DDL, `sqlite_master`), or DB
URLs. Also enforced: coordinate rounding (4 dp ≈ 11 m — enough for display, strips GPS-noise
fingerprints), file-size caps, and a JSON-schema-shaped structural check per file so an
accidental ORM-object dump cannot serialize. Publishing aborts (logged, retried next cycle) if
any check fails — fail closed.

### 4.3 Publisher — `scripts/publish_snapshot.sh` + worker hook

- **Branch mechanics:** dedicated orphan branch `snapshots` containing only the six JSON files +
  a README stub. Publish = write files into a persistent throwaway worktree
  (`data/snapshots-worktree/`, gitignored), `git add -A`, one squashed commit (message
  `snapshot <generated_at>`), `git push --force origin snapshots`. Orphan + force-push keeps the
  branch at exactly one commit — zero history bloat, and `main` is never touched. Uses the
  already-authenticated local git/gh credential; no new secrets.
- **Scheduling:** a `_maybe_publish` hook in the collector worker on
  `publish_interval_minutes` (default 3), following the exact non-blocking background-task
  pattern PR #3 established for processing (skip if a publish task is in flight; failures logged
  as `collector_publish_failed` with error class only and retried next interval; the feed never
  waits on git). Plus a manual `make publish-snapshot` for smokes and for publishing while the
  collector is stopped. Snapshot generation reads the DB with short read transactions only.
- **Failure honesty:** if publishing fails repeatedly (no network — e.g. today's AISStream-down
  scenario often accompanies captive portals), the public site simply shows a stale
  `generated_at` and the banner degrades to `collector offline` — which is the truthful display;
  no special handling needed beyond the retry.
- **Repo visibility check:** `raw.githubusercontent.com` requires the repo to be public — verify
  `jadoon200/pharos` visibility before wiring the frontend base URL; if it is private, the
  decision point is documented (make public vs. GitHub Pages from the snapshots branch — Pages
  also free; raw + public repo is the default).

### 4.4 Frontend — snapshot client + modes + Pilot view

- **`frontend/src/api.ts`:** add `SNAPSHOT_BASE =
  import.meta.env.VITE_SNAPSHOT_URL ?? 'https://raw.githubusercontent.com/jadoon200/pharos/snapshots/'`
  and typed fetchers for the six files (`snapshotStatus()`, `snapshotTracks()`, …). TanStack
  queries with `refetchInterval: 60_000`, `retry: 1`. The existing same-origin API client stays
  untouched — the baked demo keeps working exactly as deployed.
- **Mode resolution (pure function, unit-tested):** input = snapshot `status.json` fetch result +
  `generated_at`; output mode:
  - fetch failed / invalid → `demo fallback — synthetic data` (views fall back to the baked API
    data, banner amber);
  - `generated_at` age < 10 min → `live` (green);
  - 10–30 min → `delayed` (teal/neutral, shows the age);
  - > 30 min → `collector offline — showing last snapshot <age>` (grey).
  The banner renders on **every** view (in the masthead next to the existing `StatusPill`),
  always displaying `generated_at` verbatim. CDN ~5 min staleness is acceptable by design (public
  tracks are ≥ 15 min delayed anyway) and is covered by the age thresholds.
- **`views/Pilot.tsx`** (new fourth tab `Singapore Pilot`): pilot narrative header (what
  SG-PILOT-v0 is, freeze scope, day counter); ops panel from `status.json`/`stats.json`
  (observed vs calendar hours — shown side by side deliberately, reports, vessels, tracks,
  reconnects, outage list summary); model panel from `model.json` (SHA prefix, 804 params,
  threshold, freeze date); labels panel (per-source counts, review progress toward 200/50);
  evaluation panel rendering `evaluations.json` exactly as recorded — CIs, denominators,
  `not estimable` rows rendered as such, limitations verbatim, GFW attribution; a data-honesty
  footnote block (AIS coverage confound, beta feed, no-SLA statement).
- **Data-source choice per view:** Maritime Picture / Incidents / Model Report keep the baked API
  as their base data and are *not* rewired in this phase (snapshot GeoJSON tracks could feed the
  map later — recorded as a follow-up, not scope creep); the Pilot view + banner are
  snapshot-driven. This keeps the four modes simple: banner mode is global, Pilot view shows
  snapshot data or an explicit demo-fallback notice.
- Strict `tsc -b` + eslint clean; identity/styling follows the existing maritime-teal system.

### 4.5 Verification (browser, per repo discipline)

Using the repo-root dev-server launch config (`pharos-ui`) (dashboard on **:5188**, API on
:8000):

1. Unit: mode-resolution function (all four modes + boundary ages), snapshot fetcher parsing,
   Pilot view rendering with `not estimable` fixtures.
2. Sanitizer + generator tests as in 4.1/4.2 (backend gate).
3. **Four-mode browser drill:** serve fixture `status.json` variants (fresh / 20 min / 2 h /
   unreachable) via `VITE_SNAPSHOT_URL` pointed at a local static dir; screenshot each state;
   verify no view ever renders snapshot data without its banner.
4. **End-to-end publish smoke (one real run):** `make publish-snapshot` against the live DB →
   verify the `snapshots` branch on GitHub contains exactly one commit with six sanitized files →
   point the local dashboard at real `raw.githubusercontent.com` → observe `live`/`delayed` mode.
5. Render deploy check: existing `Dockerfile.web`/`render.yaml` unchanged (demo seed remains the
   fallback); redeploy picks up the new frontend; `keep-alive.yml` untouched.

### 4.6 Phase 4 sequencing

| Step | Deliverable | Depends on |
|---|---|---|
| 4a | `pharos.geo.simplify_polyline` + tests | — |
| 4b | Snapshot generator (six files) + sanitizer + tests | 4a (+3.1 only for `corroborated`/`reviewed` statuses and richer `evaluations.json` — both degrade gracefully if Phase 3 hasn't merged) |
| 4c | Publisher script + worktree/orphan-branch mechanics + worker `_maybe_publish` + manual target | 4b |
| 4d | Frontend snapshot client + mode banner + unit tests | — (parallel with 4b) |
| 4e | `views/Pilot.tsx` + tab | 4d |
| 4f | Four-mode browser drill + real publish smoke + Render redeploy + docs (DEPLOY.md, runbook, ROADMAP) | 4b–4e |

Phases 3 and 4 interleave cleanly: recommended order is 3a–3d (get labels accruing) → 4a–4f
(publish + showcase live by ~day 7) → 3e–3h (matcher/review/eval through day 13) → Phase 5
close-out on day 14.

### 4.7 Phase 4 risks

| Risk | Mitigation |
|---|---|
| snapshot leaks a secret or capability | fail-closed sanitizer with adversarial tests; outbound-only; bounded files |
| force-push race with concurrent manual publish | publisher takes a lockfile in the worktree; worker skips if a publish is in flight |
| repo turns out private → raw URL 404s | visibility check in 4.3; GitHub Pages fallback documented |
| CDN staleness misread as outage | age thresholds start at 10 min (2× CDN TTL); `generated_at` always displayed |
| git credential expires mid-pilot | publish failure logged + retried; site degrades honestly to `collector offline`; runbook notes `gh auth status` in the daily checklist |
| snapshot generation during heavy incremental cycle | read-only short transactions under WAL; background task pattern (PR #3) already proven |
| Render cold starts confuse the demo | unchanged baked-demo behaviour; banner explains mode regardless |

---

## Acceptance checklist (both phases, mapped to the v2 plan)

- [ ] Every external event during observed coverage ends `matched` / `unmatched` / `ambiguous`;
      no silent drops (Phase 3.4 preflight).
- [ ] Review queue blinded (tested), ≥ 200/≥ 50 targets tracked, 10–20 % re-review with reported
      agreement.
- [ ] Evaluation reproducible from frozen model + data + label + sampling versions; artifact SHA
      on every run; `not estimable` never rendered as zero.
- [ ] Metrics separate official / reviewed / silver / synthetic tiers; observed-time
      denominators.
- [ ] No process accepts inbound traffic; sanitizer tests prove no key/token/envelope/path/error/
      DB-metadata leakage; tracks delayed ≥ 15 min; GFW attribution present.
- [ ] Four dashboard modes browser-verified; `generated_at` always visible; laptop sleep yields
      an honest offline display.
- [ ] Backend gate + frontend `tsc -b`/eslint green on every merge; docs refreshed (runbook,
      DEPLOY.md, EVAL.md block, ROADMAP status flips).
- [ ] SG-PILOT-v0 freeze (artifact SHA, thresholds, detector config) untouched end to end.
