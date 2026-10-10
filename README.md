# Claude Running Coach

A personal running coach that lives in [Claude Code](https://claude.com/claude-code).
It reads what you actually did on Strava, owns your training plan in Google
Calendar, and talks to you in the terminal or over Telegram — a morning
brief, an evening review, a reaction minutes after each run, and answers to
whatever you ask in between.

It coaches the whole athlete, not just the running: gym sessions (with
routines in Hevy), cycling, climbing, hiking, food and fuelling, sleep and
recovery (with Apple Health). And it remembers: what it already told you,
how each week went against the plan, and the niggle you mentioned on
Tuesday.

An example of what lands on your phone:

```
Mon 07:30  Easy 6 km tonight, HR under 150. HRV 52 against your 61
           baseline after 5.9 h of sleep - keep it genuinely easy and skip
           the strides; Thursday can have them.

Mon 19:12  Clean - 145 average, 4% of the run above the ceiling, all on the
           one climb. That's 9 of the last 11 easy runs held.
```

## What it does

- **Builds your season** from your goal race and your actual history — not a
  template — and writes it into a calendar, week by week.
- **Plans each week** around your real life: it reads your personal calendar
  for travel and events, checks the weather, adjusts for niggles, and never
  lets volume grow more than ~10% on what you *actually* ran.
- **Analyses runs** properly: did it do its job, time above the easy
  ceiling, heart-rate drift, matched Strava segments, heat and terrain as
  confounders.
- **Keeps it short.** It's written to be read on a phone, one-handed. One
  main point per message.

## What you need

| | Required for |
|---|---|
| Claude Code with a Claude subscription | everything |
| Strava | everything — it's the ground truth |
| Python 3.11+ | the scripts (standard library only) |
| [uv](https://docs.astral.sh/uv/) | charts (it fetches matplotlib for them) |
| Google Calendar (claude.ai connector) | the plan in your calendar (recommended) |
| An always-on Linux or macOS machine + Telegram | check-ins on your phone |
| iPhone + Apple Watch + Health Auto Export app | sleep, HRV, recovery data |
| Hevy Pro | gym routines in the Hevy app |

Start with just Claude Code and Strava on your laptop, and add the rest when
you want it.

## Quickstart

```bash
git clone https://github.com/<you>/claude-running-coach ~/claude-running-coach
cd ~/claude-running-coach
python3 skill/scripts/coach.py init
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/running-coach
claude
```

Trust the folder when Claude Code asks, then say **"set me up"**. The coach
walks you through the rest in about half an hour: it connects to Strava,
reads your last six months, asks about your goal and your week, proposes your
heart-rate numbers and a season plan, and wires up whichever extras you want.

After that, just talk to it: "just ran", "legs feel dead", "can I move
Sunday's long run", "what should I eat before the race".

## How it's organised

```
skill/        the coach itself - shared, updated with git pull
athlete/      you: config.toml, profile.md, plan.md, and the coach's memory
docs/         deployment and customizing
```

Everything personal is in `athlete/` (gitignored), so updating is `git pull`
and backing up is making `athlete/` its own private repo.

- **[docs/customizing.md](docs/customizing.md)** — language, check-in times,
  message length, prompts, tone.
- **[docs/deployment.md](docs/deployment.md)** — Telegram, cron, Apple
  Health, voice notes, backups, troubleshooting, and the failure modes worth
  knowing about before you run this on a server.
- **[skill/SKILL.md](skill/SKILL.md)** — how the coach thinks.

## Health disclaimer

This is a training tool, not medical advice. The coach is told never to
encourage training through pain and to send you to a physio or doctor for
anything sharp, persistent or worsening — do that. Check with a doctor
before starting a new training programme if you have any health conditions.

## License

MIT — see [LICENSE](LICENSE).
