"""
pipeline/cluster.py
Pure scheduling math for game-anchored runs.

ESPN reports game times in UTC ("2026-07-11T17:00Z"); the LaunchAgent
dispatcher and the schedule files work in LOCAL wall-clock time (the
machine runs America/Los_Angeles), so everything here converts through
timezone-aware datetimes — never naive arithmetic on the UTC strings.
DST is handled by astimezone().

A "cluster" groups game start times that sit within `gap_minutes` of the
previous start; each cluster gets one full pipeline run at
`lead_minutes` before its first tip, and the run's digest covers only
that cluster's games (via --window-start/--window-end on pipeline.run).
"""

from datetime import datetime, timedelta

DEFAULT_LEAD_MINUTES = 75
DEFAULT_GAP_MINUTES = 60
DEFAULT_MAX_CLUSTER_RUNS = 3


def parse_game_time(value: str):
    """ESPN 'game_time' (e.g. '2026-07-11T17:00Z', minute precision, UTC)
    -> aware datetime in LOCAL time. Returns None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone()  # system local tz, DST-aware


def compute_clusters(starts, gap_minutes=DEFAULT_GAP_MINUTES,
                     max_runs=DEFAULT_MAX_CLUSTER_RUNS,
                     lead_minutes=DEFAULT_LEAD_MINUTES) -> list:
    """Group aware local datetimes into start-time clusters.

    Returns [{starts, run_at, window_start, window_end}] sorted by time.
    If the gap threshold yields more than max_runs clusters, adjacent
    clusters with the smallest gap are merged first (an MLB Saturday
    collapses to ~3 well-placed runs instead of 5 raggedy ones).
    """
    starts = sorted(s for s in starts if s is not None)
    if not starts:
        return []

    clusters = [[starts[0]]]
    for s in starts[1:]:
        if (s - clusters[-1][-1]) > timedelta(minutes=gap_minutes):
            clusters.append([s])
        else:
            clusters[-1].append(s)

    while len(clusters) > max_runs:
        gaps = [(clusters[i + 1][0] - clusters[i][-1], i)
                for i in range(len(clusters) - 1)]
        _, i = min(gaps)
        clusters[i:i + 2] = [clusters[i] + clusters[i + 1]]

    return [{
        "starts": c,
        "run_at": c[0] - timedelta(minutes=lead_minutes),
        "window_start": c[0],
        "window_end": c[-1],
    } for c in clusters]


def build_schedule(sport: str, date: str, game_times, schedule_cfg: dict,
                   now: datetime, morning_hour: int = 9) -> dict:
    """Build the day's run schedule for one sport.

    Runs: one 'morning' planning run over the full slate (at
    morning_hour local, or ASAP if the schedule is created later), then
    one windowed run per cluster. Clusters whose run time lands at or
    before the morning run are dropped — the morning run already covers
    them at a still-useful lead.
    """
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0,
                                              microsecond=0)
    morning_at = local_midnight + timedelta(hours=morning_hour)

    runs = [{
        "id": "morning",
        "run_at": morning_at.isoformat(),
        "window_start": None,
        "window_end": None,
        "status": "pending",
        "fired_at": None,
        "rc": None,
    }]

    clusters = compute_clusters(
        game_times,
        gap_minutes=schedule_cfg.get("gap_minutes", DEFAULT_GAP_MINUTES),
        max_runs=schedule_cfg.get("max_cluster_runs", DEFAULT_MAX_CLUSTER_RUNS),
        lead_minutes=schedule_cfg.get("lead_minutes", DEFAULT_LEAD_MINUTES),
    )
    for i, c in enumerate(clusters, 1):
        if c["run_at"] <= morning_at + timedelta(minutes=30):
            continue
        runs.append({
            "id": f"cluster-{i}",
            "run_at": c["run_at"].isoformat(),
            "window_start": c["window_start"].isoformat(),
            "window_end": c["window_end"].isoformat(),
            "status": "pending",
            "fired_at": None,
            "rc": None,
        })

    return {
        "sport": sport,
        "date": date,
        "created_at": now.astimezone().isoformat(),
        "runs": runs,
    }
