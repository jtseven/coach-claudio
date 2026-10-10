#!/usr/bin/env bash
# Nightly backup of the athlete's data - config, profile, plan and the
# coach's memory - to a PRIVATE git remote. Two layouts work
# (docs/deployment.md "Backup"):
#
#   A. athlete/ is its own private repo; the coach home is a plain checkout
#      of the public engine. This commits and pushes inside athlete/.
#   B. The coach home itself is your private repo: the engine with the
#      public repo as `upstream`, and athlete/ tracked (the `!/athlete/` line
#      in .gitignore uncommented). This commits and pushes the whole home.
#
# athlete/.gitignore keeps raw health exports out. If you add a file with
# anything secret in it, add it there FIRST. Secrets proper (.env) are
# ignored at the top level in both layouts.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ -d "$ATHLETE_DIR/.git" ]]; then
  REPO="$ATHLETE_DIR"
elif git -C "$COACH_HOME" rev-parse --git-dir >/dev/null 2>&1 \
     && ! git -C "$COACH_HOME" check-ignore -q athlete/config.toml; then
  REPO="$COACH_HOME"
else
  echo "$(now) no backup repo: make athlete/ a git repo, or track athlete/ in the coach home's private repo - see docs/deployment.md \"Backup\"" >&2
  exit 1
fi

cd "$REPO" || exit 1

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
