#!/usr/bin/env bash
# Nightly backup of the athlete's data - config, profile, plan and the
# coach's memory - to a PRIVATE git remote. Two layouts work
# (docs/deployment.md "Backup"):
#
#   A. athlete/ is its own private repo; the coach home is a plain checkout
#      of the public engine. This commits and pushes inside athlete/.
#   B. The coach home itself is your private repo: the engine with the
#      public repo as `upstream`, and athlete/ tracked (the `!/athlete/` line
#      in .gitignore uncommented). This commits and pushes the whole home -
#      and, when an `upstream` remote exists, merges the public engine's
#      updates in first (UPSTREAM_BRANCH in .env, default main).
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

# Layout B only: take the public engine's updates. Runs after the backup
# commit, so the tree is clean and a rollback can't lose athlete data. A merge
# that conflicts, or an engine that fails its own tests once merged, is rolled
# back and the athlete told - the coach keeps running on the version it had.
sync_upstream() {
  [[ "$REPO" == "$COACH_HOME" ]] || return 0
  git remote get-url upstream >/dev/null 2>&1 || return 0
  local branch="${UPSTREAM_BRANCH:-main}" before
  if ! git fetch -q upstream "$branch"; then
    echo "$(now) upstream fetch failed - engine not updated" >&2
    return 1
  fi
  if git merge-base --is-ancestor "upstream/$branch" HEAD; then
    echo "$(now) engine already up to date with upstream"
    return 0
  fi
  before=$(git rev-parse HEAD)
  if ! git merge -q --no-edit -m "merge upstream engine $(now)" "upstream/$branch"; then
    git merge --abort 2>/dev/null
    echo "$(now) upstream merge conflicts - aborted, engine not updated" >&2
    alert "⚠️ The coach engine update from upstream conflicts with local changes and was not applied. Merge it by hand: cd $COACH_HOME && git merge upstream/$branch"
    return 1
  fi
  if [[ -x "$COACH_HOME/tests/run.sh" ]] \
     && ! "$COACH_HOME/tests/run.sh" > "$LOG_DIR/upstream-tests.log" 2>&1; then
    git reset -q --hard "$before"
    echo "$(now) upstream engine failed its tests - rolled back to $(git rev-parse --short HEAD)" >&2
    alert "⚠️ The coach engine update from upstream failed its tests and was rolled back. Details in logs/upstream-tests.log on the coach machine."
    return 1
  fi
  echo "$(now) engine updated to upstream $(git rev-parse --short "upstream/$branch")"
}

git add -A
if git diff --cached --quiet; then
  echo "$(now) nothing new to back up"
else
  git commit -q -m "backup $(now)" || { echo "$(now) commit failed" >&2; exit 1; }
fi

sync_upstream

# Nothing of ours ahead of the remote: nothing to push.
if git rev-parse -q --verify "origin/$(git rev-parse --abbrev-ref HEAD)" >/dev/null \
   && [[ -z "$(git rev-list "origin/$(git rev-parse --abbrev-ref HEAD)..HEAD")" ]]; then
  exit 0
fi

if git push -q origin HEAD; then
  echo "$(now) backed up: $(git rev-parse --short HEAD)"
else
  # Committed locally but not pushed - data is still safer than before, and
  # tomorrow's run will push both. Say so rather than failing silently.
  echo "$(now) push failed (network? key?) - committed locally, will retry tomorrow" >&2
  exit 1
fi
