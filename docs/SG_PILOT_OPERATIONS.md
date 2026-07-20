# Singapore pilot operations

This runbook operates the outbound-only Singapore AIS collector on the local M3 Pro. It keeps no
public listener open. Laptop sleep and feed/network interruptions are expected: they become
coverage-outage records and are excluded from observed time rather than treated as vessel gaps.

**Current deployment:** Pilot Day 0 began on 2026-07-16. The launch agent is installed and running;
its worker commits accepted reports about every 45 seconds, processes dirty vessel tails every two
minutes with the frozen artifact SHA pinned, and schedules daily WAL-safe retention. Phases 1–2
passed the complete 117-test backend gate.

## One-time preparation

The launch agent is intentionally specific to this laptop and repository path. It runs at low I/O
priority, caps future model-processing threads at two, and writes to the local WAL database
`data/sg-live.db` (gitignored).

1. Keep the AISStream key only in the gitignored repo-root `.env`. Verify presence without printing
   it:

   ```bash
   PYTHONPATH=src /Users/jayden/anaconda3/envs/argus/bin/python -c \
     "from pharos.config import get_settings; assert get_settings().aisstream_key"
   ```

2. Create/migrate the pilot database:

   ```bash
   PHAROS_DATABASE_URL=sqlite:////Users/jayden/code-projects/pharos/data/sg-live.db \
     /Users/jayden/anaconda3/envs/argus/bin/alembic upgrade head
   ```

3. Run the deterministic mobility drill. It uses no network or key and simulates Wi-Fi loss,
   duplicate reports, a silent feed/sleep interval, and recovery:

   ```bash
   PYTHONPATH=src /Users/jayden/anaconda3/envs/argus/bin/python -m pytest -q \
     tests/test_collector_worker.py
   ```

4. Validate and install the user launch agent:

   ```bash
   plutil -lint ops/com.pharos.collector.plist
   mkdir -p ~/Library/LaunchAgents ~/Library/Logs
   cp ops/com.pharos.collector.plist ~/Library/LaunchAgents/com.pharos.collector.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pharos.collector.plist
   ```

Do not automate the macOS power setting. When plugged in for collection, enable **System Settings
→ Battery → Options → Prevent automatic sleeping on power adapter when the display is off**. On
battery or while travelling, allow normal sleep; honest partial coverage is preferable to battery
drain.

## Routine checks

Check service state and follow structured logs without exposing the key:

```bash
launchctl print gui/$(id -u)/com.pharos.collector
tail -f ~/Library/Logs/pharos-collector.log
```

Check storage, WAL integrity, the latest run, and any open outage:

```bash
du -h data/sg-live.db*
sqlite3 data/sg-live.db "PRAGMA integrity_check;"
sqlite3 -header -column data/sg-live.db \
  "SELECT id,started_at,last_message_at,status,report_count,vessel_count FROM collector_runs ORDER BY id DESC LIMIT 3;"
sqlite3 -header -column data/sg-live.db \
  "SELECT id,opened_at,closed_at,reason FROM coverage_outages ORDER BY id DESC LIMIT 5;"
```

Expected steady behavior is one commit about every 45 seconds, average CPU below 5%, modest memory,
and reconnect delays bounded at five minutes. A stale `last_message_at` with an open outage is an
honest offline state, not a dark-ship event.

Run one processing or retention cycle manually when diagnosing the scheduler:

```bash
PHAROS_DATABASE_URL=sqlite:////Users/jayden/code-projects/pharos/data/sg-live.db make process-live
PHAROS_DATABASE_URL=sqlite:////Users/jayden/code-projects/pharos/data/sg-live.db make prune
```

The incremental command refuses to score if the local GRU artifact does not match the launch
agent's pinned SHA. Pruning deletes only eligible old live positions after a successful WAL
checkpoint; it preserves tracks, incidents, labels/evaluations, and coverage records.

## Stop, restart, and update

A normal `bootout` sends termination to the worker; it flushes the current micro-batch and closes
the run/outage record:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.pharos.collector.plist
```

After code or plist changes, run the backend gate and mobility drill, copy the updated plist, then
bootstrap again. To request an immediate restart of a loaded service:

```bash
launchctl kickstart -k gui/$(id -u)/com.pharos.collector
```

If the process was killed abruptly, the next start marks the orphaned run `interrupted` at its last
valid report time. Idempotent `(mmsi, timestamp)` persistence prevents the replayed tail from being
duplicated.

## Phase 3/4 daily cadence

The import/match/health lane is **automated** by the `com.pharos.labels` launchd agent
(`ops/com.pharos.labels.plist` → `scripts/daily_labels.sh`, daily at 09:30 local, run on wake if
the laptop was asleep, logs to `~/Library/Logs/pharos-labels.log`). Each step is non-fatal and
idempotent: a GFW/network outage is recorded and simply caught up the next day. Empty imports are
valid and remain recorded as zero rather than treated as failures. Install/refresh it like the
collector agent:

```
cp ops/com.pharos.labels.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pharos.labels.plist
launchctl kickstart gui/$(id -u)/com.pharos.labels   # optional immediate run
```

What stays manual, and why:

| Cadence | Command | Purpose |
|---|---|---|
| automated daily | `make labels-import` + `labels-match` + `pilot-health` | Covered by `com.pharos.labels` |
| daily, ~20 items | `make review` | Blinded review is a *human* label — automating it would destroy the evaluation |
| after day 3 | `make review-rereview` | Queue the seeded 15% delayed blind re-review sample |
| mid-pilot, once | first `make review` run | Builds the queue and **freezes the sampling design** — do not build early |
| checkpoint/close-out | `make eval-pilot` | Record the frozen, observed-time evaluation and JSON export |
| manual smoke | `make publish-snapshot` | Generate, sanitize, squash, and push delayed snapshots (the collector also publishes every 3 min) |

ReCAAP/TSIB transcription stays manual by design (amendment 3: no APIs, no scraping).

Manual official-label checklist (about five minutes):

1. Check the ReCAAP incident/weekly map and Singapore TSIB reports page.
2. For an in-window Singapore Strait event, create one minimal file under
   `data/labels/recaap/` or `data/labels/tsib/` using the schema in
   `docs/PHASE_3_4_PLAN.md`. Include the source URL and retrieval date; copy no report prose and
   keep `notes` at or below 300 characters.
3. Run `make labels-import` and `make labels-match`. A hard validation error means the source file
   must be corrected; no event may be silently skipped.
4. Spot-check retrieval provenance during the re-review window. Never tune the frozen artifact,
   anomaly threshold, or detector settings from an in-window result.

Before public publication, run `gh auth status` and confirm that the repository is public (raw
GitHub snapshot URLs do not work for a private repository). The publisher takes an atomic lock,
uses the ignored `data/snapshots-worktree/`, and force-pushes an orphan `snapshots` commit containing
only `README.md` plus the six JSON files. A publish failure never blocks collection; the dashboard
ages into its explicit offline state.
