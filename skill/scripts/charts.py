#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.8"]
# ///
"""The coach's charts, as PNGs sized for a phone.

  weekly   The weekly log: running km vs target, hours by sport, easy runs
  load     Fitness, fatigue and form over the last months
  aerobic  Weekly time in HR zones, and easy-run efficiency
  run      One run: pace, heart rate and elevation, decoupling halves marked
  check    Confirm matplotlib loads (doctor.py uses it)

The numbers all come from analyze.py, which stays standard library only so
the coach can always compute; this file is the only one that needs a
package. uv reads the dependency block above and caches an environment for
it, so there is no venv to manage:

  uv run --script <skill>/scripts/charts.py load

Each command prints the PNG path on success. Every chart is light-surface
only: Telegram shows the image as-is, whatever the phone's theme.
"""

import argparse
import json
import sys
from datetime import date, timedelta

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import matplotlib.transforms
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
except ImportError:
    sys.exit("charts.py needs matplotlib - run it through uv:\n"
             "  uv run --script charts.py ...")

import analyze as an
import coachconf as cc

CHARTS = cc.STATE / "charts"

# The validated categorical order (blue, orange, aqua, yellow) on the light
# surface. Aqua and yellow sit under 3:1 contrast, which the legends and the
# value labels relieve.
COLORS = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "grid": "#e8e8e4",
    "muted": "#d9d8d3",
    "band": "#f0efec",
    "run": "#2a78d6",       # slot 1
    "strength": "#eb6834",  # slot 2
    "endurance": "#1baf7a", # slot 3
    "climb": "#eda100",     # slot 4
}
# HR zones are ordered, so they get one hue light -> dark (blue 250..650),
# never categorical colors.
ZONE_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]


def style(ax):
    C = COLORS
    ax.set_facecolor(C["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(C["grid"])
    ax.grid(axis="y", color=C["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=C["ink2"], length=0, labelsize=8)


def figure(rows, ratios, height):
    fig, axes = plt.subplots(rows, 1, sharex=True, figsize=(8, height), dpi=150,
                             gridspec_kw={"height_ratios": ratios, "hspace": 0.18})
    fig.patch.set_facecolor(COLORS["surface"])
    for ax in axes:
        style(ax)
    return fig, axes


def title(ax, text, sub=None):
    ax.set_title(text, loc="left", fontsize=12, color=COLORS["ink"], pad=22 if sub else 12)
    if sub:
        ax.text(0, 1.04, sub, transform=ax.transAxes, fontsize=8.5, color=COLORS["ink2"])


def ylabel(ax, text):
    ax.set_ylabel(text, fontsize=9, color=COLORS["ink2"])


def save(fig, out):
    from pathlib import Path

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=COLORS["surface"])
    plt.close(fig)
    print(out)


# --------------------------------------------------------------------------
# weekly: the weekly log
# --------------------------------------------------------------------------
#
# Sent to Telegram with the Sunday close-out. Three stacked panels on one x
# axis (never a dual-axis chart):
#   1. running km against target - the goal race's number
#   2. training hours by kind, stacked - the whole athlete: runs, rides and
#      hikes, gym, climbing. Running keeps slot 1 in both panels.
#   3. easy-run discipline, deliberately the smallest panel - still tracked,
#      no longer the headline.

#
# Sent to Telegram with the Sunday close-out. Three stacked panels on one x
# axis (never a dual-axis chart):
#   1. running km against target - the marathon's number
#   2. training hours by kind, stacked - the whole athlete: runs, rides and
#      hikes, gym, climbing.
#   3. easy-run discipline, deliberately the smallest panel - still tracked,
#      no longer the headline.
# Colors are the validated four-slot categorical order (blue, orange, aqua,
# yellow); running keeps slot 1 in both panels so it reads as one entity.
# Aqua and yellow sit under 3:1 contrast, which the legend and the total
# label on each bar relieve.

HOURS_SERIES = [  # (log field, label, color key) - bottom to top
    ("run_h", "running", "run"),
    ("other_aerobic_h", "ride / hike / other endurance", "endurance"),
    ("strength_h", "gym", "strength"),
    ("climb_h", "climbing", "climb"),
]


def cmd_weekly(args):
    try:
        with open(args.log) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        sys.exit(f"no weekly log at {args.log} - nothing logged yet")
    rows = rows[-args.last :]
    if not rows:
        sys.exit("weekly log is empty")

    C = COLORS
    labels = [
        f"W{r['week']}" if r.get("week") is not None else r.get("week_of", "?")[5:]
        for r in rows
    ]
    xs = list(range(len(rows)))
    actual = [r.get("actual_km", 0) or 0 for r in rows]
    target = [r.get("target_km", 0) or 0 for r in rows]

    fig, (ax1, axh, ax2) = plt.subplots(
        3, 1, sharex=True, figsize=(8, 7.2), dpi=150,
        gridspec_kw={"height_ratios": [3, 2.6, 1.1], "hspace": 0.18},
    )
    fig.patch.set_facecolor(C["surface"])

    for ax in (ax1, axh, ax2):
        style(ax)

    # -- 1. running volume: actual bars, target as a neutral tick --
    ax1.bar(xs, actual, width=0.62, color=C["run"], zorder=3)
    for x, km, t in zip(xs, actual, target):
        if km:
            y = max(km, t) if abs(km - t) < 2.5 else km
            ax1.annotate(
                f"{km:.0f}", (x, y), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=8, color=C["ink"], zorder=4,
            )
    for x, t in zip(xs, target):
        if t:
            ax1.hlines(t, x - 0.38, x + 0.38, color=C["ink2"], linewidth=2, zorder=4)
    ax1.set_ylabel("running km", fontsize=9, color=C["ink2"])
    ax1.set_title("Training, week by week", loc="left", fontsize=12,
                  color=C["ink"], pad=12)
    handles = [Patch(facecolor=C["run"], label="ran")]
    if any(target):
        handles.append(Line2D([], [], color=C["ink2"], linewidth=2, label="target"))
    ax1.legend(handles=handles, loc="upper left", frameon=False, fontsize=8,
               labelcolor=C["ink2"], handlelength=1.4)

    # -- 2. hours by kind, stacked. Weeks logged before the split carry no
    #       per-kind fields and are left empty rather than guessed. --
    split = [any(f in r for f, _, _ in HOURS_SERIES) for r in rows]
    totals = [sum((r.get(f) or 0) for f, _, _ in HOURS_SERIES) if ok else 0
              for r, ok in zip(rows, split)]
    # One holiday week (24 h on a bike tour) would flatten every normal week, so
    # the axis caps at the ordinary range and an over-cap bar says its total.
    ordinary = sorted(t for t in totals if t)
    cap = max(8.0, (ordinary[-2] if len(ordinary) > 1 else ordinary[-1] if ordinary else 8) * 1.35)
    bottoms = [0.0] * len(rows)
    for f, lab, ck in HOURS_SERIES:
        vals = [(r.get(f) or 0) if ok else 0 for r, ok in zip(rows, split)]
        if not any(vals):
            continue
        shown = [max(0.0, min(v, cap - b)) for v, b in zip(vals, bottoms)]
        axh.bar(xs, shown, width=0.62, bottom=bottoms, color=C[ck],
                edgecolor=C["surface"], linewidth=1, zorder=3, label=lab)
        bottoms = [b + v for b, v in zip(bottoms, shown)]
    for x, t, ok in zip(xs, totals, split):
        if ok and t:
            over = t > cap
            axh.annotate(
                f"{t:.0f} h" + (" ↑" if over else ""), (x, min(t, cap)),
                textcoords="offset points", xytext=(0, 3), ha="center",
                fontsize=8, color=C["ink"], zorder=4,
            )
    axh.set_ylim(0, cap * 1.15)
    axh.set_ylabel("hours\nby sport", fontsize=9, color=C["ink2"])
    axh.legend(loc="upper left", frameon=False, fontsize=7.5, ncol=4,
               labelcolor=C["ink2"], handlelength=1.2, columnspacing=1.0,
               bbox_to_anchor=(0, 1.12))

    # -- 3. easy-run discipline, small and neutral --
    pts = [
        (x, r["easy_runs_under_ceiling"] / r["easy_runs_rated"] * 100,
         f"{r['easy_runs_under_ceiling']}/{r['easy_runs_rated']}")
        for x, r in zip(xs, rows)
        if r.get("easy_runs_rated")
    ]
    ax2.set_ylim(-15, 135)
    ax2.set_yticks([0, 100])
    ax2.set_yticklabels(["0%", "100%"], fontsize=7)
    ax2.set_ylabel("easy runs\nheld", fontsize=8, color=C["ink2"])
    if pts:
        ax2.plot([p[0] for p in pts], [p[1] for p in pts], color=C["ink2"],
                 linewidth=1.5, marker="o", markersize=5,
                 markeredgecolor=C["surface"], markeredgewidth=1.2, zorder=3)
        for x, y, lab in pts:
            ax2.annotate(lab, (x, y), textcoords="offset points", xytext=(0, 6),
                         ha="center", fontsize=7, color=C["ink2"], zorder=4)
    else:
        ax2.text(0.5, 0.5, "no easy runs rated yet", transform=ax2.transAxes,
                 ha="center", fontsize=8, color=C["ink2"])

    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels, fontsize=8)
    ax2.set_xlim(-0.6, len(rows) - 0.4)

    save(fig, args.out)


# --------------------------------------------------------------------------
# load: fitness, fatigue, form
# --------------------------------------------------------------------------
#
# Two panels. Fitness and fatigue share a unit (load per day) and an axis,
# with the daily loads as recessive bars behind them. Form gets its own
# panel as % of fitness - the scale-free number the bands are judged on -
# with the productive band shaded neutral and labelled rather than colored.


def cmd_load(args):
    C = COLORS
    until = date.fromisoformat(args.until) if args.until else date.today()
    series = an.daily_load_series(an.read_load_log(), until)
    if not series:
        sys.exit("no training load saved yet - analyze.py load --activities ... --save")
    first_day = series[0][0]
    series = series[-args.days:]
    days = [d for d, _, _, _ in series]
    loads = [l for _, l, _, _ in series]
    ctl = [c for _, _, c, _ in series]
    atl = [a for _, _, _, a in series]
    # Form as % of a fitness that is still seeding means nothing - not drawn.
    seed_end = first_day + timedelta(days=an.CTL_DAYS)
    form = [(c - a) / c * 100 if c > 1 and d >= seed_end else None
            for d, c, a in zip(days, ctl, atl)]

    fig, (ax1, ax2) = figure(2, [3, 1.8], 6)
    ax1.bar(days, loads, width=0.8, color=C["muted"], zorder=2)
    ax1.plot(days, ctl, color=C["run"], linewidth=2, zorder=3)
    ax1.plot(days, atl, color=C["strength"], linewidth=1.5, zorder=3)
    # End labels, nudged apart: the two lines often finish close together.
    gap = 5
    for vals, lab, dy in ((ctl, "fitness", -gap if ctl[-1] <= atl[-1] else gap),
                          (atl, "fatigue", gap if ctl[-1] <= atl[-1] else -gap)):
        ax1.annotate(f"{lab} {vals[-1]:.0f}", (days[-1], vals[-1]), textcoords="offset points",
                     xytext=(6, dy), va="center", fontsize=8, color=C["ink"])
    ax1.legend(handles=[Line2D([], [], color=C["run"], linewidth=2, label="fitness (42-day)"),
                        Line2D([], [], color=C["strength"], linewidth=1.5, label="fatigue (7-day)"),
                        Patch(facecolor=C["muted"], label="daily load")],
               loc="upper left", frameon=False, fontsize=8, labelcolor=C["ink2"], ncol=3)
    top = max(max(loads), max(atl), max(ctl)) or 1
    ax1.set_ylim(0, top * 1.25)
    ylabel(ax1, "load per day\n(Strava relative effort)")
    now = f"form {form[-1]:+.0f}% of fitness" if form[-1] is not None else "form not judged yet"
    title(ax1, "Observed training load (coverage may be incomplete)", f"CTL {ctl[-1]:.0f}, ATL {atl[-1]:.0f}, {now} "
          f"- after {until.isoformat()}")

    # Fit the data (within ±100%) so nothing is clipped into a flat line.
    shown = [f for f in form if f is not None] or [0]
    lo = max(-100, min(-50, min(shown) - 8))
    hi = min(100, max(40, max(shown) + 8))
    ax2.axhspan(-30, -10, color=C["band"], zorder=1)
    ax2.axhline(0, color=C["grid"], linewidth=1, zorder=2)
    ax2.plot(days, [None if f is None else max(-100, min(100, f)) for f in form],
             color=C["ink2"], linewidth=1.5, zorder=3)
    # Band names in the right margin, where the line can't run over them.
    margin = matplotlib.transforms.blended_transform_factory(ax2.transAxes, ax2.transData)
    for y, lab in ((-20, "negative balance"), ((lo - 30) / 2, "large negative"), (15, "positive balance")):
        ax2.text(1.01, y, lab, transform=margin, va="center", fontsize=7, color=C["ink2"])
    ax2.axhline(-30, color=C["grid"], linewidth=0.8, linestyle=":", zorder=2)
    # Big blocks on low fitness can go far past ±100%: say where it's cut.
    for i, f in enumerate(form):
        if f is not None and abs(f) > 100 and (i == 0 or form[i - 1] is None
                                                or abs(form[i - 1]) <= 100):
            ax2.annotate(f"{'<' if f < 0 else '>'} {'-' if f < 0 else '+'}100%", (days[i], 100 if f > 0 else -100),
                         textcoords="offset points", xytext=(4, 4 if f < 0 else -10),
                         fontsize=7, color=C["ink2"])
    ax2.set_ylim(lo, hi)
    ticks = [t for t in (-100, -30, 0, 25, 50, 100) if lo <= t <= hi]
    ax2.set_yticks(ticks)
    ax2.set_yticklabels([f"{t:+d}%" if t else "0" for t in ticks])
    ylabel(ax2, "model balance\n(not injury risk)")
    if seed_end > days[0]:
        end = min(seed_end, days[-1])
        for ax in (ax1, ax2):
            ax.axvspan(days[0], end, color=C["band"], alpha=0.6, zorder=0)
        ax2.text(days[0] + (end - days[0]) / 2, 15, "fitness still seeding,\nform not shown",
                 ha="center", va="center", fontsize=7, color=C["ink2"])
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
    save(fig, args.out)


# --------------------------------------------------------------------------
# aerobic: time in zones, easy-run efficiency
# --------------------------------------------------------------------------
#
# Weekly hours in each HR zone, stacked, with the share at or under the easy
# ceiling labelled on each bar. Below, the weekly median efficiency of easy
# runs - the slow, honest sign the aerobic work is landing. Different
# measures, so different panels.


def cmd_aerobic(args):
    C = COLORS
    rows = an.read_run_log()
    zones = dict(an.intensity_weeks(rows, args.weeks))
    eff = dict(an.efficiency_weeks(rows, args.weeks))
    weeks = sorted(set(zones) | set(eff))[-args.weeks:]
    if not weeks:
        sys.exit("run log is empty - analyze.py streams --append adds runs")
    xs = list(range(len(weeks)))
    labels = [date.fromisoformat(w).strftime("%d %b") for w in weeks]

    fig, (ax1, ax2) = figure(2, [3, 1.6], 6)
    zone_labels = next((z["zone_labels"] for z in zones.values() if z["zone_labels"]), None)
    if zone_labels:
        n = len(zone_labels)
        ramp = ZONE_RAMP if n == 5 else [ZONE_RAMP[round(i * 4 / max(n - 1, 1))] for i in range(n)]
        bottoms = [0.0] * len(weeks)
        for i, lab in enumerate(zone_labels):
            vals = [((zones.get(w) or {}).get("zone_s") or [0] * n)[i] / 3600 for w in weeks]
            ax1.bar(xs, vals, bottom=bottoms, width=0.62, color=ramp[i],
                    edgecolor=C["surface"], linewidth=1, zorder=3, label=lab)
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        for x, w, b in zip(xs, weeks, bottoms):
            z = zones.get(w)
            tot = z and (z["low"] + z["mid"] + z["high"])
            if tot:
                ax1.annotate(f"{z['low'] / tot * 100:.0f}%", (x, b), textcoords="offset points",
                             xytext=(0, 3), ha="center", fontsize=8, color=C["ink"], zorder=4)
        ax1.set_ylim(0, max(bottoms) * 1.3 or 1)
        ax1.legend(loc="upper left", frameon=False, fontsize=7.5, ncol=n,
                   labelcolor=C["ink2"], handlelength=1.2, columnspacing=1.0)
    else:
        ax1.text(0.5, 0.5, "no HR zones configured", transform=ax1.transAxes,
                 ha="center", fontsize=8, color=C["ink2"])
    ylabel(ax1, "running hours\nby HR zone")
    title(ax1, "Observed aerobic trends", "Classified HR time only; missing runs/sensors and terrain limit inference")

    pts = [(x, eff[w]["ef"]) for x, w in zip(xs, weeks) if w in eff and eff[w]["ef"]]
    ceiling = cc.easy_ceiling()
    if pts:
        ax2.plot([p[0] for p in pts], [p[1] for p in pts], color=C["run"], linewidth=2,
                 marker="o", markersize=5, markeredgecolor=C["surface"], markeredgewidth=1.2,
                 zorder=3)
        ys = [p[1] for p in pts]
        pad = max((max(ys) - min(ys)) * 0.6, 0.05)
        ax2.set_ylim(min(ys) - pad, max(ys) + pad)
        for x, y in (pts[0], pts[-1]) if len(pts) > 1 else (pts[0],):
            lab = f"{y:.2f}"
            if ceiling:
                lab += f" ≈ {an.fmt_pace(60000 / (y * ceiling))}/km @ {ceiling:g}"
            ax2.annotate(lab, (x, y), textcoords="offset points", xytext=(0, 7),
                         ha="center", fontsize=7, color=C["ink2"], zorder=4)
    else:
        ax2.text(0.5, 0.5, "no easy runs logged yet", transform=ax2.transAxes,
                 ha="center", fontsize=8, color=C["ink2"])
    ylabel(ax2, "easy-run EF\n(m/min per beat)")
    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels)
    ax2.set_xlim(-0.6, len(weeks) - 0.4)
    save(fig, args.out)


# --------------------------------------------------------------------------
# run: one activity's streams
# --------------------------------------------------------------------------
#
# Pace, heart rate and elevation on one distance axis. The warm-up that
# decoupling leaves out is shaded and the halves are split by a line, so the
# drift number in the title can be seen rather than taken on trust.


def rolling(vals, k):
    out = []
    for i in range(len(vals)):
        win = [v for v in vals[max(0, i - k + 1):i + 1] if v is not None]
        out.append(sum(win) / len(win) if win else None)
    return out


def cmd_run(args):
    C = COLORS
    st = an.parse_streams(an.read_json(args.streams))
    samples = an.stream_samples(st)
    ceiling = args.ceiling or cc.easy_ceiling()
    res = an.analyse_streams(st, ceiling, args.skip_warmup)

    # x: distance in km where there is a distance stream, else minutes.
    if st.get("distance"):
        xs = [s["d"] / 1000 for s in samples]
        xlabel = "km"
    else:
        acc, xs = 0, []
        for s in samples:
            acc += s["dt"]
            xs.append(acc / 60)
        xlabel = "minutes"

    # The same warm-up and halfway points decoupling() uses.
    steady_total = res.get("decoupling", {}).get("steady_s", 0)
    skipped = acc = 0
    warm_x = half_x = None
    for x, s in zip(xs, samples):
        if not s["moving"] or not s["hr"] or s["hr"] <= 0:
            continue
        if skipped < args.skip_warmup:
            skipped += s["dt"]
            warm_x = x
            continue
        acc += s["dt"]
        if half_x is None and acc >= steady_total / 2:
            half_x = x

    k = max(1, round(60 / (sum(s["dt"] for s in samples) / max(len(samples), 1))))
    pace = [1000 / s["v"] / 60 if s["moving"] and s["v"] and s["v"] > 1 else None
            for s in samples]
    pace = rolling(pace, k)
    hr = [s["hr"] if s["hr"] and s["hr"] > 0 else None for s in samples]
    alt = [s["alt"] for s in samples]

    panels = 3 if any(a is not None for a in alt) else 2
    fig, axes = figure(panels, [2.4, 2.4, 1.2][:panels], 6.6 if panels == 3 else 5.4)
    axp, axh = axes[0], axes[1]

    axp.plot(xs, [p if p is None else min(p, 12) for p in pace], color=C["run"], linewidth=1.5)
    axp.invert_yaxis()
    axp.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: an.fmt_pace(v * 60)))
    ylabel(axp, "pace /km")

    axh.plot(xs, hr, color=C["strength"], linewidth=1.5, zorder=3)
    if ceiling:
        axh.axhline(ceiling, color=C["ink2"], linewidth=1, linestyle="--", zorder=2)
        axh.annotate(f"ceiling {ceiling:g}", (xs[0], ceiling), textcoords="offset points",
                     xytext=(2, 3), fontsize=7, color=C["ink2"])
    ylabel(axh, "heart rate")

    if panels == 3:
        axe = axes[2]
        base = min(a for a in alt if a is not None)
        axe.fill_between(xs, [a if a is not None else base for a in alt], base,
                         color=C["muted"], linewidth=0)
        ylabel(axe, "elevation m")
    axes[-1].set_xlabel(xlabel, fontsize=8, color=C["ink2"])

    d = res.get("decoupling", {})
    if "pct" in d:
        for ax in axes:
            if warm_x:
                ax.axvspan(xs[0], warm_x, color=C["band"], zorder=0)
            if half_x:
                ax.axvline(half_x, color=C["ink2"], linewidth=0.8, linestyle=":", zorder=1)
        h1, h2 = d["halves"]
        # Each half's average, along the bottom where no line can cover it.
        foot = matplotlib.transforms.blended_transform_factory(axh.transData, axh.transAxes)
        for (lo, hi), h in (((warm_x or xs[0], half_x), h1), ((half_x, xs[-1]), h2)):
            axh.text((lo + hi) / 2, 0.05, f"avg {h['avg_hr']:.0f} bpm", transform=foot,
                     ha="center", fontsize=7.5, color=C["ink"])
        sub = (f"decoupling {d['pct']:+.1f}% ({d['basis']}:HR) - shaded: warm-up left out, "
               "dotted: halfway")
    else:
        sub = "decoupling: " + d.get("reason", res.get("note", "n/a"))
    head = f"{res['km']:.1f} km in {an.fmt_time(res['moving_s'])}"
    if res.get("avg_hr"):
        head += f", avg HR {res['avg_hr']:.0f}"
    title(axp, args.title or head, sub)
    save(fig, args.out)


def cmd_check(_args):
    print(f"matplotlib {matplotlib.__version__}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("weekly", help="the weekly log as a PNG")
    s.add_argument("--log", default=str(cc.WEEKLY_LOG))
    s.add_argument("--last", type=int, default=12, help="how many weeks to plot")
    s.add_argument("--out", default=str(CHARTS / "weekly.png"))
    s.set_defaults(func=cmd_weekly)

    s = sub.add_parser("load", help="fitness, fatigue and form")
    s.add_argument("--days", type=int, default=120)
    s.add_argument("--until", help="YYYY-MM-DD, default today")
    s.add_argument("--out", default=str(CHARTS / "load.png"))
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("aerobic", help="weekly HR zones and easy-run efficiency")
    s.add_argument("--weeks", type=int, default=12)
    s.add_argument("--out", default=str(CHARTS / "aerobic.png"))
    s.set_defaults(func=cmd_aerobic)

    s = sub.add_parser("run", help="one run's pace, HR and elevation")
    s.add_argument("--streams", required=True,
                   help="get_activity_streams JSON: a file path, '-' for stdin, or the JSON")
    s.add_argument("--ceiling", type=float, help="default: physiology.easy_ceiling")
    s.add_argument("--skip-warmup", type=int, default=600)
    s.add_argument("--title", help="headline, e.g. 'Sunday long run'")
    s.add_argument("--out", default=str(CHARTS / "run.png"))
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("check", help="confirm matplotlib loads")
    s.set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
