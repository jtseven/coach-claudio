#!/usr/bin/env python3
"""Writes a synthetic get_activity_streams response: a 75-minute steady run
at constant speed and power whose heart rate drifts upward, with a short
stop in the middle. Deterministic, so the expected numbers are stable.

    make_streams.py OUT.json [--no-watts]
"""

import json
import math
import sys

out = sys.argv[1]
watts = "--no-watts" not in sys.argv

t, hr, v, w, dist, alt, moving = [], [], [], [], [], [], []
d = 0.0
for i in range(0, 75 * 6 + 1):          # a sample every 10 s
    sec = i * 10
    stopped = 2400 <= sec < 2460        # a minute at a crossing
    speed = 0.0 if stopped else 2.8
    d += speed * 10 if i else 0
    t.append(sec)
    v.append(speed)
    moving.append(not stopped and i > 0)
    # Warm-up ramp to 140, then ~0.15 bpm per minute of drift.
    base = 110 + 30 * min(sec, 600) / 600
    hr.append(round(base + max(0, sec - 600) / 60 * 0.15, 1))
    w.append(0 if stopped else 200 + round(3 * math.sin(i / 7)))
    dist.append(round(d, 1))
    alt.append(round(150 + 8 * math.sin(i / 40), 1))

streams = {"time": t, "heart_rate": hr, "velocity_smooth": v, "distance": dist,
           "altitude": alt, "moving": moving}
if watts:
    streams["watts"] = w
with open(out, "w") as fh:
    json.dump(streams, fh)
