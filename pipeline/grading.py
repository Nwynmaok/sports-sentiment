"""
pipeline/grading.py
Grades the previous run's suggestions against final scores (ESPN) and
keeps a running record per confidence band in data/<sport>/state/record.json.

Spreads and totals grade from final scores. Player props need box-score
stats we don't fetch yet — they're reported as ungraded.
"""

import json
import logging
from pathlib import Path

from adapters.odds import espn

log = logging.getLogger("pipeline.grading")


def _load_json(path: Path, default):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _grade_one(s: dict, scores: dict):
    """Returns 'win' | 'loss' | 'push' | None (ungradeable)."""
    game = scores.get(s["game"])
    if not game or game.get("state") != "post":
        return None
    hs, as_ = game.get("home_score"), game.get("away_score")
    if hs is None or as_ is None:
        return None
    line = s.get("line")
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None

    if s["market"] == "total":
        total = hs + as_
        if total == line:
            return "push"
        went_over = total > line
        picked_over = s["side"] == "over/home"
        return "win" if went_over == picked_over else "loss"

    if s["market"] == "spread":
        # line is the home team's line; home covers if margin + line > 0
        cover = (hs - as_) + line
        if cover == 0:
            return "push"
        home_covered = cover > 0
        picked_home = s["side"] == "over/home"
        return "win" if home_covered == picked_home else "loss"

    return None  # props: no box scores yet


def grade_previous(cfg, data_dir: Path, today: str):
    """Grade the most recent un-graded suggestions file before today.
    Returns a short digest line (or None if nothing to grade)."""
    sug_dir = data_dir / "suggestions"
    if not sug_dir.exists():
        return None
    state_path = data_dir / "state" / "record.json"
    record = _load_json(state_path, {"graded_dates": [], "bands": {}})

    candidates = sorted(p.stem for p in sug_dir.glob("*.json"))
    candidates = [d for d in candidates
                  if d < today and d not in record["graded_dates"]]
    if not candidates:
        return None
    date = candidates[-1]

    data = _load_json(sug_dir / f"{date}.json", {})
    suggestions = data.get("suggestions", [])
    if not suggestions:
        record["graded_dates"].append(date)
        _save(state_path, record)
        return None

    scores = {r["matchup"]: r for r in espn.fetch_scores(cfg, date)}

    results = []
    wins = losses = pushes = ungraded = 0
    for s in suggestions:
        outcome = _grade_one(s, scores)
        if outcome is None:
            ungraded += 1
            continue
        mark = {"win": "✅", "loss": "❌", "push": "➖"}[outcome]
        results.append(f"{s['pick']} {mark}")
        band = record["bands"].setdefault(s["confidence"], {"w": 0, "l": 0, "p": 0})
        if outcome == "win":
            wins += 1
            band["w"] += 1
        elif outcome == "loss":
            losses += 1
            band["l"] += 1
        else:
            pushes += 1
            band["p"] += 1

    record["graded_dates"] = (record["graded_dates"] + [date])[-30:]
    _save(state_path, record)

    if not results:
        return None
    lines = [f"Yesterday ({date}): {wins}-{losses}"
             + (f"-{pushes}" if pushes else "")
             + (f" · {ungraded} ungraded" if ungraded else "")]
    lines.append(" · ".join(results[:6]))
    band_bits = []
    for band in ("A", "B", "C"):
        b = record["bands"].get(band)
        if b and (b["w"] or b["l"]):
            band_bits.append(f"{band} {b['w']}-{b['l']}")
    if band_bits:
        lines.append("Record: " + ", ".join(band_bits))
    log.info(f"Graded {date}: {wins}-{losses}-{pushes}, {ungraded} ungraded")
    return "\n".join(lines)


def _save(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=1)
