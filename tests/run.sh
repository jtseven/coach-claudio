#!/usr/bin/env bash
# Smoke tests for the coach's scripts. No accounts, no network, no claude:
# everything runs against a scratch COACH_HOME built from tests/fixtures.
#
#   tests/run.sh
#
# Exit 0 when everything passes.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="$REPO/tests/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export COACH_HOME="$TMP/home"
mkdir -p "$COACH_HOME"
ln -s "$REPO/skill" "$COACH_HOME/skill"
S="$REPO/skill/scripts"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); echo "  ok    $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL  $1"; [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/        /'; }

# expect "<name>" "<needle>" cmd...  - command succeeds and prints needle
expect() {
  local name="$1" needle="$2"; shift 2
  local out
  if out=$("$@" 2>&1) && grep -qF -- "$needle" <<<"$out"; then ok "$name"; else bad "$name" "$out"; fi
}
# expect_fail "<name>" "<needle>" cmd...  - command fails and prints needle
expect_fail() {
  local name="$1" needle="$2"; shift 2
  local out
  if ! out=$("$@" 2>&1) && grep -qF -- "$needle" <<<"$out"; then ok "$name"; else bad "$name" "$out"; fi
}

echo "syntax"
for f in "$S"/*.sh; do
  if bash -n "$f"; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f")"; fi
done
for f in "$S"/*.py; do
  if python3 -m py_compile "$f" 2>/dev/null; then ok "compile $(basename "$f")"; else bad "compile $(basename "$f")"; fi
done
find "$S" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

echo "before setup"
expect_fail "check without config" "no athlete config" python3 "$S/coach.py" check
expect "init creates athlete/" "athlete/config.toml" python3 "$S/coach.py" init
expect_fail "check with blank template" "incomplete" python3 "$S/coach.py" check
expect "init is idempotent" "nothing, all present" python3 "$S/coach.py" init

echo "configured"
cp "$FIX/config.toml" "$COACH_HOME/athlete/config.toml"
# Plan starts four Mondays before this week's Monday, so today is W5.
MONDAY=$(python3 -c "from datetime import date,timedelta as t;d=date.today();print(d-t(days=d.weekday()+28))")
sed -i.bak "s/__PLAN_START__/$MONDAY/" "$COACH_HOME/athlete/config.toml" && rm -f "$COACH_HOME/athlete/config.toml.bak"
expect "check passes" "ok" python3 "$S/coach.py" check
expect "show names the athlete" "Alex (she/her)" python3 "$S/coach.py" show
expect "show computes plan week" "W5/20" python3 "$S/coach.py" show
expect "get scalar" "152" python3 "$S/coach.py" get physiology.easy_ceiling
expect "get list" "EBikeRide" python3 "$S/coach.py" get activities.commute_types
expect "week of plan start is W1" "W1/20" python3 "$S/coach.py" week --date "$MONDAY"
D4=$(python3 -c "from datetime import date,timedelta as t;print(date.fromisoformat('$MONDAY')+t(days=21))")
expect "W4 is a down week" "(down week)" python3 "$S/coach.py" week --date "$D4"

echo "prompts"
expect "morning prompt names athlete" "Give Alex the morning brief" python3 "$S/coach.py" prompt morning
expect "morning prompt carries language" "Write in Spanish" python3 "$S/coach.py" prompt morning
out=$(python3 "$S/coach.py" prompt morning)
if grep -q "health.py summary" <<<"$out"; then bad "apple_health off hides recovery block"; else ok "apple_health off hides recovery block"; fi
if grep -q "{{" <<<"$out"; then bad "no template tags left" "$out"; else ok "no template tags left"; fi
expect "postrun prompt carries activity" "Strava activity 123 (sport type:" python3 "$S/coach.py" prompt postrun --activity 123 --sport Run
expect "check_in.sh --print-prompt" "Mode: evening check-in" "$S/check_in.sh" evening --print-prompt
mkdir -p "$COACH_HOME/athlete/prompts"
echo "Custom prompt for {name}" > "$COACH_HOME/athlete/prompts/morning.md"
expect "athlete prompt override wins" "Custom prompt for Alex" python3 "$S/coach.py" prompt morning
rm -r "$COACH_HOME/athlete/prompts"
# Nested conditionals resolve innermost first.
out=$(cd "$S" && python3 -c "
import coach
print(coach.render('a{{#if x}}b{{#if y}}c{{/if}}d{{/if}}e{{#unless x}}f{{/unless}}', {'x': True, 'y': False}, {}))")
if [[ "$out" == "abde" ]]; then ok "nested conditionals"; else bad "nested conditionals" "$out"; fi

echo "deployment files"
expect "crontab has morning at 06:45" "45 6 * * *" python3 "$S/coach.py" crontab
expect "crontab has watchdog" "ensure_channel.sh" python3 "$S/coach.py" crontab
expect "hooks json" "telegram_typing.py start" python3 "$S/coach.py" hooks

echo "coachlog"
expect "empty log" "log is empty" python3 "$S/coachlog.py" recent
expect "add" "logged" python3 "$S/coachlog.py" add --mode morning --ids 111 --note "said keep it easy"
expect "covered" "covered" python3 "$S/coachlog.py" covered 111
expect_fail "not covered" "not covered" python3 "$S/coachlog.py" covered 222
expect_fail "rejects long note" "limit 300" python3 "$S/coachlog.py" add --mode morning --note "$(printf 'x%.0s' {1..301})"
expect_fail "rejects bad mode" "mode must be" python3 "$S/coachlog.py" add --mode lunch --note "hi"
expect "dry run writes nothing" "dry run" env COACH_DRY_RUN=1 python3 "$S/coachlog.py" add --mode evening --ids 333 --note "test"
expect_fail "dry run really wrote nothing" "not covered" python3 "$S/coachlog.py" covered 333
expect "niggle" "recorded" python3 "$S/coachlog.py" niggle --area "left knee" --severity 2 --note "downhills"
expect "niggles lists open" "left knee" python3 "$S/coachlog.py" niggles
expect "resolve" "resolved" python3 "$S/coachlog.py" niggle --area "Left Knee" --resolve
expect "resolved is hidden" "no open niggles" python3 "$S/coachlog.py" niggles

echo "analyze"
expect "vdot" "VDOT" python3 "$S/analyze.py" vdot --distance 10000 --time 2700
expect "paces default from config" "VDOT 40" python3 "$S/analyze.py" paces
expect "configured zones" "configured zones" python3 "$S/analyze.py" zones
expect "zones show ceiling" "Easy ceiling 152" python3 "$S/analyze.py" zones
expect "derived zones for other HRmax" "DERIVED" python3 "$S/analyze.py" zones --hrmax 190
expect "ceiling default from config" "Ceiling 152" python3 "$S/analyze.py" ceiling --hr "[140,150,160,145]" --time "[0,60,120,180]"
expect "decouple without band" "No personal easy-run band" python3 "$S/analyze.py" decouple --watts 200 --hr 145
expect "week rollup" "Ceiling for next week" python3 "$S/analyze.py" week --activities "$FIX/activities.json"
expect "weeklog prints entry" '"actual_km": 21.0' python3 "$S/analyze.py" weeklog --activities "$FIX/activities.json" --week-of 2026-09-07 --week 1 --target-km 20 --target-lr 10 --easy-above 5,30
out=$(python3 "$S/analyze.py" weeklog --activities "$FIX/activities.json" --week-of 2026-09-07 --week 1 --target-km 20 --easy-above 5,30)
if grep -q '"emtb_h": 0.5' <<<"$out" && grep -q '"easy_runs_under_ceiling": 1' <<<"$out"; then ok "weeklog splits commute, rates by time"; else bad "weeklog splits commute, rates by time" "$out"; fi
expect "weeklog --append" "appended" python3 "$S/analyze.py" weeklog --append --activities "$FIX/activities.json" --week-of 2026-09-07 --week 1 --target-km 20 --target-lr 10
expect_fail "weeklog refuses duplicate" "already in the weekly log" python3 "$S/analyze.py" weeklog --append --activities "$FIX/activities.json" --week-of 2026-09-09 --week 1
expect "trend" "Ceiling for next week (+10% on last actual): 23.1 km" python3 "$S/analyze.py" trend
expect "block" "Block W1-W1" python3 "$S/analyze.py" block

echo "health"
expect "summary without data" "no health data yet" python3 "$S/health.py" summary
python3 "$FIX/make_health.py" "$COACH_HOME/athlete/state/health/daily"
expect "summary with data" "Resting HR" python3 "$S/health.py" summary
expect "trend" "Resting HR" python3 "$S/health.py" trend --metric resting_heart_rate

echo "weather (offline parts)"
expect "weather needs no network for --help" "forecast" python3 "$S/weather.py" --help


echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
