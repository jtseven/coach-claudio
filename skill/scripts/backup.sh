#!/usr/bin/env bash
# Nightly backup of athlete/ - config, profile, plan and the coach's memory -
# to its own PRIVATE git remote. The engine is a public checkout you can
# always clone again; athlete/ is the part that can't be recreated.
#
# One-time setup (docs/deployment.md "Backup"):
#   cd athlete && git init -b main && git remote add origin <private repo>
#
# athlete/.gitignore keeps raw health exports out. If you add a file with
# anything secret in it, add it there FIRST. Secrets proper (.env) live in
# the coach home, outside athlete/, so they can't be committed from here.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cd "$ATHLETE_DIR" || exit 1
if [[ ! -d .git ]]; then
  echo "$(now) athlete/ is not a git repo - see docs/deployment.md \"Backup\"" >&2
  exit 1
fi

git add -A
if git diff --cached --quiet; then
  echo "$(now) nothing new to back up"
  exit 0
fi

git commit -q -m "backup $(now)" || { echo "$(now) commit failed" >&2; exit 1; }

if git push -q origin HEAD; then
  echo "$(now) backed up: $(git rev-parse --short HEAD)"
else
  # Committed locally but not pushed - data is still safer than before, and
  # tomorrow's run will push both. Say so rather than failing silently.
  echo "$(now) push failed (network? key?) - committed locally, will retry tomorrow" >&2
  exit 1
fi
