#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
worktree="$repo_root/data/snapshots-worktree"
lockdir="$repo_root/data/.snapshot-publish.lock"

if ! mkdir "$lockdir" 2>/dev/null; then
  echo "snapshot publish already in flight" >&2
  exit 0
fi
trap 'rmdir "$lockdir" 2>/dev/null || true' EXIT

if [[ ! -e "$worktree/.git" ]]; then
  mkdir -p "$(dirname "$worktree")"
  git -C "$repo_root" worktree add --detach "$worktree" HEAD
fi

git -C "$worktree" switch --detach --discard-changes >/dev/null
if git -C "$repo_root" show-ref --verify --quiet refs/heads/snapshot-publish; then
  git -C "$repo_root" branch -D snapshot-publish >/dev/null
fi
git -C "$worktree" switch --orphan snapshot-publish >/dev/null
git -C "$worktree" rm -rf --ignore-unmatch . >/dev/null

# $PYTHON is set by the collector worker (its own interpreter); launchd's default PATH has no
# `python`, so resolving explicitly keeps worker-scheduled publishes working.
PYTHONPATH="$repo_root/src" "${PYTHON:-python3}" -m pharos.publish.snapshot --out-dir "$worktree"
printf '%s\n' '# PHAROS delayed public snapshots' '' \
  'Generated outbound-only from the SG-PILOT ledger. Tracks are delayed and all incidents are human-review candidates.' \
  > "$worktree/README.md"

git -C "$worktree" add -- README.md status.json stats.json tracks.json incidents.json evaluations.json model.json
generated_at="$(sed -n 's/.*"generated_at": "\([^"]*\)".*/\1/p' "$worktree/status.json" | head -1)"
git -C "$worktree" commit -m "snapshot $generated_at" >/dev/null
git -C "$worktree" push --force origin HEAD:snapshots
