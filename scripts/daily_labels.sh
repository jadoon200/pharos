#!/usr/bin/env bash
# Daily automated label pass: GFW import → matching → ops/drift health summary.
# Runs as the com.pharos.labels launchd agent. Each step is independent and non-fatal:
# a GFW/network outage must never block matching of already-stored events or the
# health log — failures are recorded and the next day's run simply catches up
# (importer and matcher are idempotent). ReCAAP/TSIB YAML entry and blinded review
# remain deliberately manual (no source APIs; human labels are the point).
set -uo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-python3}"
export PYTHONPATH="$repo_root/src"

echo "=== daily labels pass $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
status=0

if ! "$python" -m pharos.labels.external; then
  echo "labels-import failed (non-fatal; retried tomorrow)" >&2
  status=1
fi
if ! "$python" -m pharos.labels.match; then
  echo "labels-match failed (non-fatal; retried tomorrow)" >&2
  status=1
fi
if ! "$python" -m scripts.pilot_health; then
  echo "pilot-health failed (non-fatal; retried tomorrow)" >&2
  status=1
fi

echo "=== daily labels pass done (status=$status) ==="
exit "$status"
