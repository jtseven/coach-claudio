"""Writes 30 days of synthetic Health Auto Export day files ending today.

Usage: make_health.py <daily dir>. Used by tests/run.sh only.
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
random.seed(1)
for i in range(30):
    d = date.today() - timedelta(days=i)
    stamp = f"{d.isoformat()} 07:00:00 +0000"
    metrics = {
        "resting_heart_rate": [{"date": stamp, "qty": 52 + random.randint(-2, 2)}],
        "heart_rate_variability": [{"date": stamp, "qty": 60 + random.randint(-8, 8)}],
        "respiratory_rate": [{"date": stamp, "qty": 14.5 + random.random()}],
        "vo2_max": [{"date": stamp, "qty": 48.0}],
    }
    (out / f"{d.isoformat()}.json").write_text(json.dumps({"metrics": metrics}))
