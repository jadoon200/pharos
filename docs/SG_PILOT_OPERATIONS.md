# Singapore pilot operations

This runbook operates the outbound-only Singapore AIS collector on the local M3 Pro. It keeps no
public listener open. Laptop sleep and feed/network interruptions are expected: they become
coverage-outage records and are excluded from observed time rather than treated as vessel gaps.

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
