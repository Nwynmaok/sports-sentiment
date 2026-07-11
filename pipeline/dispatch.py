"""
pipeline/dispatch.py
Game-anchored run dispatcher. A LaunchAgent runs this every 15 minutes
(`com.wynclaw.sports-sentiment.dispatch`); it replaces the old fixed
9:00/16:00 per-sport agents.

Each tick, for every sport whose config has schedule.enabled:

  1. If today's schedule doesn't exist yet (first tick at/after 07:30
     local), fetch the slate's start times from ESPN (keyless, no SGO
     quota) and build it: one 'morning' planning run at 09:00 over the
     full slate, plus one windowed run per start-time cluster at T-75
     minutes (pipeline/cluster.py). Times are stored as local-offset ISO
     strings; ESPN's UTC timestamps are converted, DST-safe.
  2. Fire any pending run whose time has come, sequentially, as
     `python3 -m pipeline.run --sport S [--window-start --window-end]`.
     Sequential firing plus the shared timeline cache means coinciding
     MLB/WNBA cluster runs pay for Twitter once.
     A run whose whole window is already 15+ minutes past tip is marked
     'missed' instead of fired (nothing actionable to say).

If the machine was asleep, launchd fires the next tick on wake and
overdue runs catch up then. A pid lockfile keeps overlapping ticks from
double-firing while a pipeline run is in flight.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sport_config import load_sport, available_sports  # noqa: E402
from adapters.odds import espn  # noqa: E402
from pipeline.cluster import parse_game_time, build_schedule  # noqa: E402

log = logging.getLogger("pipeline.dispatch")

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "_shared" / "dispatch.lock"
CREATE_ANCHOR_HOUR = 7
CREATE_ANCHOR_MINUTE = 30
MORNING_HOUR = 9
RUN_TIMEOUT_S = 40 * 60
MISSED_GRACE_MIN = 15


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)  # raises if no such process
            log.info(f"tick skipped: dispatch pid {pid} still running")
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            log.warning("removing stale dispatch lock")
    LOCK.write_text(str(os.getpid()))
    return True


def _release_lock():
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


def _schedule_path(sport: str, date: str) -> Path:
    return ROOT / "data" / sport / "state" / f"run_schedule_{date}.json"


def _save_schedule(sched: dict):
    path = _schedule_path(sched["sport"], sched["date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sched, indent=1))


def ensure_schedule(sport: str, now: datetime, dry_run=False):
    """Create today's schedule on the first tick at/after the anchor."""
    date = now.strftime("%Y-%m-%d")
    path = _schedule_path(sport, date)
    if path.exists():
        return json.loads(path.read_text())
    anchor = now.replace(hour=CREATE_ANCHOR_HOUR, minute=CREATE_ANCHOR_MINUTE,
                         second=0, microsecond=0)
    if now < anchor:
        return None

    cfg = load_sport(sport)
    slate = espn.fetch_game_data(cfg, date)
    times = [parse_game_time(g.get("game_time", ""))
             for g in slate["games"].values()]
    sched = build_schedule(sport, date, times, cfg.raw.get("schedule", {}),
                           now, morning_hour=MORNING_HOUR)
    if not slate["games"]:
        sched["runs"] = []  # no slate -> nothing to do today
    log.info(f"{sport}: schedule created — {len(slate['games'])} games, "
             f"{len(sched['runs'])} runs")
    if not dry_run:
        _save_schedule(sched)
    return sched


def fire_due(sport: str, sched: dict, now: datetime, dry_run=False):
    for run in sched["runs"]:
        if run["status"] != "pending":
            continue
        if datetime.fromisoformat(run["run_at"]) > now:
            continue

        if run["window_end"]:
            last_tip = datetime.fromisoformat(run["window_end"])
            if now > last_tip + timedelta(minutes=MISSED_GRACE_MIN):
                run["status"] = "missed"
                log.warning(f"{sport}/{run['id']}: window already past, marking missed")
                if not dry_run:
                    _save_schedule(sched)
                continue

        argv = [sys.executable, "-m", "pipeline.run",
                "--sport", sport, "--format", "markdown"]
        if run["window_start"]:
            argv += ["--window-start", run["window_start"],
                     "--window-end", run["window_end"]]
        log.info(f"{sport}/{run['id']}: firing ({' '.join(argv[2:])})")
        if dry_run:
            continue

        run["status"] = "running"
        run["fired_at"] = now.isoformat()
        _save_schedule(sched)
        try:
            proc = subprocess.run(argv, cwd=str(ROOT), timeout=RUN_TIMEOUT_S)
            run["rc"] = proc.returncode
            run["status"] = "done" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            run["rc"] = -1
            run["status"] = "failed"
            log.error(f"{sport}/{run['id']}: timed out after {RUN_TIMEOUT_S}s")
        _save_schedule(sched)


def tick(now: datetime = None, dry_run=False):
    now = (now or datetime.now()).astimezone()
    for sport in available_sports():
        cfg = load_sport(sport)
        if not cfg.raw.get("schedule", {}).get("enabled"):
            continue
        # runs marked 'running' by a dispatcher that died stay running
        # forever otherwise; fold them to failed after the run timeout.
        sched = ensure_schedule(sport, now, dry_run=dry_run)
        if not sched:
            continue
        for run in sched["runs"]:
            if run["status"] == "running" and run.get("fired_at"):
                fired = datetime.fromisoformat(run["fired_at"])
                if now - fired > timedelta(seconds=RUN_TIMEOUT_S + 300):
                    run["status"] = "failed"
                    if not dry_run:
                        _save_schedule(sched)
        fire_due(sport, sched, now, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Game-anchored run dispatcher")
    parser.add_argument("--now", help="Override current time (ISO, for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan and log, but write nothing and fire nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    now = datetime.fromisoformat(args.now).astimezone() if args.now else None

    if args.dry_run:
        tick(now, dry_run=True)
        return
    if not _acquire_lock():
        return
    try:
        tick(now)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
