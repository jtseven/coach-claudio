# Deployment

How the coach runs on a machine: what's needed, where things live, and the
handful of non-obvious failure modes that took real outages to find. The
setup conversation (`skill/references/setup.md`) walks an athlete through the
parts they need; this is the reference behind it.

Any always-on machine that runs Linux or macOS works: a Raspberry Pi 4/5, an
old laptop, a NUC or home server, a small VPS. Nothing here is Pi-specific.
For chat-only use (no scheduled check-ins), your everyday computer is enough.

## Contents

- Requirements
- Layout
- Install
- Environment
- MCP servers
- State files
- Telegram
- Two processes, one bot token
- Post-run reactions
- Apple Health via Health Auto Export
- Weekly chart
- Voice messages
- Cron and time zones
- macOS notes
- OAuth without a browser
- Backup
- Updating
- Token budget
- Troubleshooting

## Requirements

- **Claude Code**, logged in (`claude`, then `/login`). The scheduled runs
  reuse that login; no `ANTHROPIC_API_KEY` is needed or used.
- **Python 3.11+** (the scripts use only the standard library; `tomllib` is
  why 3.11).
- `git`, `curl`.
- For Telegram: `tmux`, and the Telegram plugin (it runs on `bun`, which the
  plugin installs or tells you to install).
- For Hevy: Node.js (`npx`).
- For the weekly chart: a venv with matplotlib. For voice notes: `ffmpeg` and
  whisper.cpp.

## Layout

```
claude-running-coach/       <- this repo (the "coach home")
├── skill/                  <- the engine, symlinked into ~/.claude/skills/
│   ├── SKILL.md
│   ├── references/         <- coaching method: playbook, plan mechanics, ...
│   ├── prompts/            <- scheduled check-in prompts (overridable)
│   ├── templates/          <- what `coach.py init` copies into athlete/
│   └── scripts/
├── athlete/                <- gitignored: everything about one athlete
│   ├── config.toml
│   ├── profile.md
│   ├── plan.md
│   ├── prompts/            <- optional per-athlete prompt overrides
│   └── state/
│       ├── checkin_log.jsonl   <- what the coach has already said
│       ├── weekly_log.jsonl    <- planned vs actual, one line per week
│       ├── niggles.jsonl       <- pain/niggle reports, open and resolved
│       ├── postrun_seen.txt    <- activity ids the post-run watcher handled
│       └── health/daily/       <- Apple Health exports, one JSON per day
├── .env                    <- gitignored secrets
├── venv/                   <- optional, matplotlib for the weekly chart
├── vendor/                 <- optional, whisper.cpp for voice notes
├── outbox/                 <- messages that failed to send
└── logs/
```

The engine is shared; `athlete/` is the only thing that is yours. That's
what makes updating (`git pull`) and backing up (`athlete/` as its own
private repo) simple.

## Install

```bash
git clone <this repo> ~/claude-running-coach
cd ~/claude-running-coach
python3 skill/scripts/coach.py init
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/running-coach
claude      # trust this folder when asked, then say: "set me up"
```

Trusting the folder matters beyond convenience: the persistent Telegram
session starts here, and an untrusted folder makes it hang on the trust
dialog forever (see `ensure_channel.sh`, failure mode 5).

## Environment

`.env` in the coach home, `chmod 600`, created by `coach.py init` from
`skill/templates/env.example`:

```bash
export CLAUDE_BIN="$HOME/.local/bin/claude"   # absolute; cron's PATH is minimal
export TELEGRAM_CHAT_ID="<numeric chat id>"
export HEALTH_API_KEY="<openssl rand -hex 16>"
```

The Telegram bot token deliberately lives elsewhere, with the plugin, at
`~/.claude/channels/telegram/.env` — not duplicated here. The scripts source
both (`skill/scripts/lib.sh`).

Cron runs with a minimal environment and will not source your shell profile,
so the scripts source `.env` explicitly and set their own `PATH`. That's the
single most common reason a scheduled check-in silently does nothing.

## MCP servers

**Strava** (required, read-only) — activities, streams, performance detail,
zones, gear. Register it under exactly this name, because the post-run
watcher refers to it:

```bash
claude mcp add --scope user --transport http strava-mcp https://mcp.strava.com/mcp
```

then authenticate once with `/mcp` in an interactive session. (A claude.ai
Strava connector also works for conversation and check-ins, but the post-run
watcher can't use account connectors.)

**Google Calendar** (recommended, read/write) — the claude.ai account
connector: claude.ai → Settings → Connectors → Google Calendar. Nothing to
install on the machine; it comes with the Claude login, in conversation and
in cron alike.

**Hevy** (optional, read/write) — gym routines and history. Needs Hevy Pro
for an API key:

```bash
claude mcp add --scope user hevy -e HEVY_API_KEY=<key> -- npx -y hevy-mcp
```

**Telegram** — the channel plugin, see below.

**No weather MCP server, deliberately.** `weather.py` calls Open-Meteo over
plain HTTPS. It needs no key, so an MCP server would wrap a single GET in a
process whose tool definitions load into *every* session — permanent token
cost for something used a few times a day — and add another MCP lifecycle to
a setup where MCP lifecycle is the main source of outages.

## State files

Three append-only logs in `athlete/state/`, created by `coach.py init` and
`check_in.sh` if missing. None is load-bearing for delivery — if one is lost,
the coach still works, it just repeats itself or loses the trend.

**`checkin_log.jsonl`** — one line per message that discussed specific
activity data, so the next run doesn't re-tell it. Always written through
`coachlog.py add`, which caps notes at 300 characters and validates the mode:
left alone, notes grow into kilobytes of reasoning and every read drags them
into context.

**`weekly_log.jsonl`** — one line per completed week, written by the weekly
close-out through `analyze.py weeklog --append`, which refuses a duplicate
week.

**`niggles.jsonl`** — pain and niggle reports via `coachlog.py niggle`.
Latest line per `area` wins; a resolved line closes one.

**`postrun_seen.txt`** — the post-run watcher's memory of handled activity
ids. Deleting it is safe: the next poll re-initialises without triggering
anything.

**A test run writes to these exactly like a real one**, and the next real
check-in then skips whatever the test "covered". Test with
`check_in.sh <mode> --no-send`, which sets `COACH_DRY_RUN=1`: every write is
skipped and says so.

## Telegram

What you get: chat with the coach from your phone (text, and voice notes if
enabled), plus the morning brief, the evening review, post-run reactions and
failure alerts.

1. **Create a bot**: message @BotFather on Telegram, `/newbot`, keep the token.
2. **Install the plugin** in an interactive `claude` session:
   `/plugin install telegram@claude-plugins-official`, then
   `/telegram:configure <token>`.
3. **Pair**: message your bot, then `/telegram:access` to approve yourself.
   Only allow-listed accounts can talk to the coach.
4. **Chat id**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` after
   messaging the bot and copy `message.chat.id` into `.env` as
   `TELEGRAM_CHAT_ID`.
5. **Switch it on**: `integrations.telegram = true` in `athlete/config.toml`.
6. **Cron**: `python3 skill/scripts/coach.py crontab` prints the full crontab
   for this machine (check-ins at the configured times, post-run watcher,
   watchdogs). Install it with `crontab -e` (paste) or, if you have no other
   cron jobs, `coach.py crontab | crontab -`.
7. **Typing indicator (optional)**: a coaching turn takes 30–120 s while it
   reads Strava, the calendar and Hevy, and Telegram's "typing…" lasts 5 s.
   `telegram_typing.py` keeps it alive, driven by hooks. Merge the output of
   `coach.py hooks` into `~/.claude/settings.json` — merge, don't overwrite;
   that file carries your other settings. It only ever types for real
   Telegram turns, never for a terminal session, and every failure exits 0.
8. **Start it**: `skill/scripts/ensure_channel.sh` (cron does this on boot and
   every 5 minutes). `tmux attach -t running-coach-telegram` to watch,
   `Ctrl-b d` to detach.
9. **Test**: send the bot a real message and watch a reply arrive. That is the
   only valid test (next section).

## Two processes, one bot token

Telegram allows exactly one `getUpdates` poller per bot token. The plugin's
server enforces this itself — on boot it kills whatever process holds the
lock file, on the assumption it's a stale orphan, without checking whether
it's alive and well.

And the plugin is enabled **globally**, so it auto-connects for *every*
claude session on the machine — not just ones launched with `--channels`.
Any session — an unrelated terminal, a one-off `claude -p`, even `claude mcp
list` — connects to Telegram just by starting and steals the poll lock from
the live bot.

The obvious fix is wrong, and it fails in a way that looks like success, so
read this before "simplifying" it:

The plugin must stay **enabled globally**. `--channels` resolves its provider
from enabled plugins; disable the plugin and the persistent session still
prints its channel banner and still starts a polling server, but the channel
never binds. Inbound messages are then fetched from Telegram and silently
dropped — Telegram considers them delivered, so they're gone. Loading it
per-session with `--plugin-dir` does **not** work around this either.

**Every health signal reads green while the bot is deaf**: `bot.pid` is live,
the bun process holds connections to Telegram, `getWebhookInfo` shows
`pending_update_count: 0` (because the poller *is* consuming your messages),
the banner prints, and the `reply` tools are loaded. Tool availability and
message delivery are different things, and only the second is the feature.
**The only valid test is sending the bot a real message and watching it
arrive.**

So the split is the other way round from what you'd guess:

- The **persistent session** launches plain, with the plugin enabled:
  `claude --channels plugin:telegram@claude-plugins-official`, in a detached
  tmux session kept alive by `ensure_channel.sh`. It is the only thing that
  ever holds the polling slot.
- **Every scheduled run** disables the plugin for itself:
  `--settings '{"enabledPlugins":{"telegram@claude-plugins-official":false}}'`.
  Strava, Hevy and Google Calendar still load. With no telegram tool, the
  coach replies in plain text and `check_in.sh` sends it with `curl` to the
  Bot API.

**Residual risk, accepted:** any *other* claude session started on the
machine still steals the poller. The watchdog recovers within ~10 minutes
(`/reload-plugins` first, a full restart if that doesn't take). For other
work on the coach machine, start claude with the same `--settings` override.

Two more ways the bot goes deaf, both handled by `ensure_channel.sh`:

- **A plugin auto-update under a running session.** The server restarts on
  the new version and polls happily, but the channel binding doesn't survive.
  The watchdog records the version the session started with and restarts on
  a mismatch.
- **An untrusted start folder.** The session hangs on the trust dialog and
  the watchdog's `/reload-plugins` then answers it with "No, exit". Trust
  the coach folder once, interactively.

### Dead ends, so nobody re-walks them

- **Disabling the plugin globally and re-adding it per session with
  `--plugin-dir`**: looks clean, silently breaks inbound delivery.
- **`--strict-mcp-config`** for the check-ins: keeps telegram out, but also
  excludes claude.ai account connectors, so Google Calendar disappears. (The
  post-run watcher does use it — it only needs Strava.)
- **`--disallowedTools` naming every telegram tool**: does **not** prevent the
  connection.
- **`--bare`**: requires `ANTHROPIC_API_KEY`, refuses the OAuth login, and
  skips skill discovery.
- **Telegram through the MCP tool from cron**: any second session loading the
  plugin kills the persistent poller. Cron talks to the Bot API with `curl`.

## Post-run reactions

`post_run_watch.sh`, from cron every 30 minutes during waking hours, makes
feedback arrive minutes after a workout instead of at the evening review. Two
stages, split by cost:

1. **Detect** — a Haiku `-p` run whose only job is to list the last three
   activities as JSON, under `--strict-mcp-config` with an inline Strava-only
   server (which is what keeps it off the Telegram poller). It reuses the
   stored login of the user-scope `strava-mcp` server.
2. **React** — only for a new activity: `check_in.sh postrun <id> <type>`,
   the full coaching pipeline. The coach replies `SKIP` if the activity was
   already discussed.

Commutes (`activities.commute_types`) and anything older than 12 hours never
trigger it. Roughly 30 polls a day at a few thousand tokens each.

## Apple Health via Health Auto Export

Needs an iPhone, ideally an Apple Watch, and the paid **Health Auto Export –
JSON+CSV** app. The watch's sleep, HRV, resting HR, respiratory rate, wrist
temperature, VO2max and running form then reach the coach, so it reads
recovery instead of asking about it.

The receiving side:

- `health_server.py` — a standard-library HTTP server on port 5556
  (`HEALTH_PORT`), kept alive by `ensure_health_server.sh` from cron. It
  stores each day's metrics in `athlete/state/health/daily/YYYY-MM-DD.json`.
  Re-exports overwrite per metric (idempotent), except sleep, which is merged
  because the app sometimes re-sends a night as a shorter fragment.
- `health.py` — what the coach reads through. Never sum or average a metric
  by hand: `value()` knows that steps, energy and distance accumulate while
  rates average. Averaging what should be summed once reported a day's active
  energy as 80 kJ instead of 1754.

**iPhone setup, one time:**

1. Install the app and grant it Health access (sleep, HRV, resting heart rate
   at minimum — everything is better).
2. Create an automation: type **REST API**, the URL (below), format JSON.
3. Add a header `x-api-key` with the value of `HEALTH_API_KEY` from `.env`.
4. Two automations to the same URL with the same header work best: one with
   aggregation **none (raw)** for the vitals (heart rate, HRV, resting HR,
   respiratory rate, blood oxygen, wrist temperature, cardio recovery,
   VO2max, running form, body composition), and one **hourly** for sleep and
   the accumulating metrics (steps, energy, distance). Raw steps/energy arrive
   about once a second and from two devices at once; hourly aggregation
   dedupes them, and the server folds any raw ones it gets into hourly sums.
5. Sync every hour or two, and make sure one export lands before the morning
   brief — the app only runs while the phone is unlocked now and then.
6. "Export now", then `python3 skill/scripts/health.py summary`.

**Reaching the machine from the phone** — pick one:

- **Home Wi-Fi only**: `http://<machine-ip>:5556/health`. Simplest; exports
  just wait until you're home.
- **Tailscale** (recommended): install it on the machine and the phone, then
  use `http://<machine-tailscale-name>:5556/health`. Works from anywhere,
  exposes nothing to the internet.
- **Reverse proxy on a domain** (Caddy, nginx): proxy `/healthexport*` to
  `localhost:5556` over HTTPS — the server answers on `/healthexport` too.
  Avoid Cloudflare's proxy for this host: it caps request bodies at 100 MB
  and a first export with months of heart rate exceeds that.

To test the server without an iPhone:

```bash
source .env
curl -s -X POST http://localhost:5556/health -H "x-api-key: $HEALTH_API_KEY" \
  -d '{"data":{"metrics":[{"name":"resting_heart_rate","units":"bpm","data":[{"date":"2026-08-14 07:00:00 +0200","qty":48}]}]}}'
```

**Delete the test file from `athlete/state/health/daily/` afterwards** —
synthetic values poison the 28-day baseline.

Health data stays on the machine: `athlete/.gitignore` keeps it out of the
backup. Losing it costs baseline depth, not function.

## Weekly chart

The weekly close-out can be followed by a PNG: running km vs target,
training hours by sport, and easy-run discipline, rendered from the weekly
log by `analyze.py chart`. matplotlib usually can't be installed system-wide
(PEP 668), so:

```bash
python3 -m venv venv && venv/bin/pip install matplotlib
```

and `integrations.weekly_chart = true`. Every failure is non-fatal — the text
message always outranks the picture.

## Voice messages

Telegram voice notes, transcribed locally with whisper.cpp — nothing leaves
the machine. Needs `ffmpeg` and a build:

```bash
git clone https://github.com/ggerganov/whisper.cpp vendor/whisper.cpp
cd vendor/whisper.cpp && cmake -B build && cmake --build build -j --config Release
sh ./models/download-ggml-model.sh small
```

`small` is a good balance on a Pi 5 or anything faster (roughly real-time);
use `base` on slower machines (`WHISPER_MODEL` in `.env`). Then
`integrations.voice = true`.

## Cron and time zones

`coach.py crontab` renders everything from the config. Cron fires in the
**machine's** local time, so it has to match `athlete.timezone`, or a machine
on a UTC default fires the morning brief hours off. `doctor.py` checks this.

- Linux: `sudo timedatectl set-timezone Europe/Berlin`, and make sure cron
  runs at boot (`sudo systemctl enable cron`; on some distros it's `cronie`).
- macOS: System Settings → General → Date & Time.

The weekly planning and close-out are a day-of-week branch inside the evening
check-in, not separate cron entries; the days are `schedule.plan_day` and
`schedule.closeout_day`. Changing check-in *times* means re-rendering and
reinstalling the crontab.

## macOS notes

The scripts avoid GNU-only tools (`date -I`, `flock`, `timeout`, `nproc`), so
they run on stock macOS. Two things to know:

- Install `coreutils` (`brew install coreutils`) to get `gtimeout`; without
  it the 15-minute backstop on a hung check-in is skipped.
- cron needs **Full Disk Access** if the repo lives under Documents, Desktop
  or iCloud (System Settings → Privacy & Security → Full Disk Access → add
  `/usr/sbin/cron`). Keeping the repo in your home folder avoids that. The
  Mac must also be awake at check-in time.

## OAuth without a browser

Strava's MCP login wants a browser, which a headless box doesn't have. The
auth URL redirects to a `localhost` callback on the machine running claude,
so either:

1. **Forward the callback port.** Start the login on the box, note the port
   in the callback URL, then from your laptop
   `ssh -L <port>:localhost:<port> you@box` and open the auth URL in your
   laptop's browser. The callback reaches the box through the tunnel.
2. **Authenticate on a machine with a browser** and copy the credentials
   across. Check where your Claude Code version stores MCP credentials
   before doing this.

Google Calendar needs nothing on the box — it's the claude.ai account
connector.

Verify before trusting the schedule (from a session started with the
`--settings` override if Telegram is already running):

```bash
skill/scripts/check_in.sh morning --no-send
```

## Backup

The engine is a public checkout you can always clone again. `athlete/` is the
irreplaceable part: config, profile, plan and the logs. Make it its own
private repo and push it nightly:

```bash
cd athlete
git init -b main
git remote add origin git@github.com:<you>/<private-repo>.git
git add -A && git commit -m "initial" && git push -u origin main
```

Then re-render the crontab — the backup line is active once `athlete/.git`
exists. `athlete/.gitignore` keeps raw health data out, and secrets live in
the coach home's `.env`, outside `athlete/`, so they can't be committed from
there. If a secret ever lands in a commit, rotate it (Telegram: @BotFather;
health key: a new `openssl rand -hex 16` and update the phone) — history
rewriting on a backup repo is not worth trusting.

Restore: clone the engine, clone your athlete repo into `athlete/`, recreate
`.env`, `coach.py crontab`.

## Updating

```bash
git pull
python3 skill/scripts/doctor.py
```

`athlete/` is untouched by updates. If you've overridden a prompt in
`athlete/prompts/`, compare it with the new `skill/prompts/` version.

## Token budget

Two check-ins a day at roughly 40–80k tokens each, ~30 Haiku detect polls (a
few thousand tokens each), and a full post-run reaction on training days:
around 150–250k tokens a day, plus whatever you chat. That fits a Claude
Pro/Max subscription's usage comfortably, but it's worth knowing before
you're surprised by it.

## Troubleshooting

Start with `python3 skill/scripts/doctor.py`.

**Failure alerts.** `lib.sh` turns known failures into a fix in the Telegram
warning — an expired Claude login says "run `claude`, then /login" instead
of "exit 1". The post-run watcher alerts once after four failed polls in a
row, and once when it recovers.

**Nothing arrives at check-in time.** Check `logs/morning.log`. Usually the
environment: `CLAUDE_BIN` wrong, or the machine's timezone.

**Runs but doesn't send.** Check `outbox/` — every failure path (non-zero
exit, timeout, empty message, failed send) writes the output there, with the
coach's stderr after a `--- stderr ---` line.

**Sends, but the content is wrong or empty.** Usually an MCP that failed to
authenticate, in which case the coach should be saying so. An empty message
is caught and diverted to `outbox/`.

**Telegram messages get no reply.** The poller is down. `ensure_channel.sh`
self-heals within 10 minutes; if it doesn't, read `logs/channel.log`. Did you
just start another claude session on the machine? That's the usual cause.

**The coach repeats itself, or contradicts an earlier message.** Look at
`coachlog.py recent`. Either it isn't being logged, or a test run wrote
entries for messages that were never sent.

**Clock drift** on a box without a real-time clock: `timedatectl status`,
"System clock synchronized: yes".
