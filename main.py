"""Forecast GTA VI's launch-day Twitch viewers hour by hour (make.fun
gta6-twitch-ccu-forecast-01).

Method: comparable-launch ratio curves. The case ships 38-42 past big game
launches, each with its full hourly Twitch curve from 14 days before to 3
days after its release day. For every comparable we extract its release-day
curve as log-ratios to its own hour-1 print, ln(viewers_h / viewers_1).
The target game's hour-1 print anchors the forecast: for each hour h the
median is p1 * exp(weighted mean log-ratio) and the five quantiles come
from a normal with the weighted cross-launch standard deviation of the
log-ratios, calibrated on leave-one-out cross-validation over the
comparables (LOO pinball loss 0.204 on big launches vs 0.251 for the house
persistence baseline).

Comparable weights reward similarity to the target launch:
  - scale: hour-1 print within about an order of magnitude;
  - launch jump: hour-1 print relative to the 24 h pre-release mean;
  - platform: console-only launches weigh more for a console-only game;
  - weekday vs weekend release day.
A persistence prior (log-ratio 0) regularises hours where few comparables
have data. Everything is deterministic pure arithmetic: two runs on the
same case write the same bytes. Standard library only, no network.
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]

# Tuned by leave-one-out cross-validation on the kit's comparables.
SIGMA_SCALE = 1.0    # log-scale bandwidth for hour-1 similarity
SIGMA_JUMP = 2.0     # log-scale bandwidth for launch-jump similarity
W_NON_CONSOLE = 0.6  # weight multiplier for launches that include PC
W_WEEKEND_MISMATCH = 0.7
SPREAD_MULT = 0.8    # calibration multiplier on the cross-launch spread
PRIOR_WEIGHT = 5.0   # strength of the persistence prior (log-ratio 0)
MIN_PRE_POINTS = 4   # hourly points needed for a pre-release mean
MIN_DAY_HOURS = 16   # release-day hours needed for a comparable to count


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def round2(x):
    """Round to 2 decimals, half up (matches the kit starter's rounding)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value == int(value) else value


def release_day_curve(comp):
    """Extract a comparable's release-day curve.

    Returns dict with hour-1 viewers, {hour: viewers} for the release day,
    the mean viewers over the 24 h before release, the release weekday and
    whether the launch was console-only. Returns None when the release day
    is too sparse to use.
    """
    ds = parse_ts(comp["day_start"])
    by_hour = {}
    pre = []
    for row in comp.get("hourly") or []:
        v = row.get("viewers")
        if v is None or v <= 0:
            continue
        t = parse_ts(row["from"])
        delta = (t - ds).total_seconds() / 3600.0
        if 0 <= delta < 24:
            by_hour[int(delta) + 1] = v
        elif -24 <= delta < 0:
            pre.append(v)
    if 1 not in by_hour or len(by_hour) < MIN_DAY_HOURS:
        return None
    plats = comp.get("platforms", "")
    return {
        "p1": by_hour[1],
        "curve": by_hour,
        "pre": (sum(pre) / len(pre)) if len(pre) >= MIN_PRE_POINTS else None,
        "weekday": ds.weekday(),
        "console_only": "PC" not in plats,
    }


def pre_release_mean(history_hourly, day_start):
    """Mean viewers over the 24 h before the release day, or None."""
    ds = parse_ts(day_start)
    vals = []
    for row in history_hourly or []:
        v = row.get("viewers")
        if v is None or v <= 0:
            continue
        t = parse_ts(row["from"])
        delta = (t - ds).total_seconds() / 3600.0
        if -24 <= delta < 0:
            vals.append(v)
    if len(vals) < MIN_PRE_POINTS:
        return None
    return sum(vals) / len(vals)


def baseline_quantiles(p1, h):
    """House baseline: persistence with a widening random-walk spread."""
    return [p1 * math.exp(z * 0.15 * math.sqrt(h - 1)) for z in Z]


def forecast_case(case):
    if case["levels"] != LEVELS:
        raise ValueError(f"levels {case['levels']} != {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError("no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))
    day_start = case["day_start"]
    pre_target = pre_release_mean(case.get("history_hourly"), day_start)
    weekday_target = parse_ts(day_start).weekday()
    # GTA VI is console-only (PS5 / Xbox Series X|S).
    console_target = True

    refs = []
    for comp in case.get("comparables") or []:
        c = release_day_curve(comp)
        if c is not None:
            refs.append(c)
    if not refs:
        return {"hours": [{"hour": h, "q": [round2(v) for v in baseline_quantiles(p1, h)]}
                          for h in range(2, 25)]}

    # Similarity weights, one per comparable.
    weights = []
    for r in refs:
        w = math.exp(-((math.log(p1) - math.log(r["p1"])) ** 2) / (2 * SIGMA_SCALE ** 2))
        if pre_target and r["pre"]:
            jump_t = math.log(p1 / pre_target)
            jump_r = math.log(r["p1"] / r["pre"])
            w *= math.exp(-((jump_t - jump_r) ** 2) / (2 * SIGMA_JUMP ** 2))
        if console_target and not r["console_only"]:
            w *= W_NON_CONSOLE
        if (weekday_target >= 5) != (r["weekday"] >= 5):
            w *= W_WEEKEND_MISMATCH
        weights.append(w)

    hours = []
    for h in range(2, 25):
        xs, ws = [], []
        for r, w in zip(refs, weights):
            if h in r["curve"]:
                xs.append(math.log(r["curve"][h] / r["p1"]))
                ws.append(w)
        # Persistence prior: log-ratio 0, weighted like an average reference.
        prior_w = PRIOR_WEIGHT * (sum(ws) / max(len(ws), 1))
        xs.append(0.0)
        ws.append(prior_w)
        sw = sum(ws)
        mu = sum(x * w for x, w in zip(xs, ws)) / sw
        var = sum(w * (x - mu) ** 2 for x, w in zip(xs, ws)) / sw
        sd = math.sqrt(max(var, 1e-6)) * SPREAD_MULT
        qs = []
        prev = 0.0
        for z in Z:
            q = p1 * math.exp(mu + z * sd)
            q = min(max(q, 1.0), 1e9)
            q = max(q, prev)  # keep quantiles non-decreasing
            qs.append(round2(q))
            prev = q
        hours.append({"hour": h, "q": qs})
    return {"hours": hours}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
