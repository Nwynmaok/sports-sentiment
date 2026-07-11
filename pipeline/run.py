"""
pipeline/run.py — daily sentiment pipeline, sport-agnostic.

Usage:
    python3 -m pipeline.run --sport mlb
    python3 -m pipeline.run --sport nba --date 2026-10-25 --format markdown

Steps:
    1. Odds: SportsGameOdds (props, if SGO_API_KEY) else ESPN (lines, keyless)
    2. Build social search queries from the slate
    3. Fetch posts: Reddit + Bluesky (free) + twitterapi.io (optional)
    4. Match posts to games/props, aggregate sentiment, build alerts

Outputs under data/<sport>/:
    queries/game_data_<date>.json   slate + lines + props
    raw/<date>.json                 game-keyed normalized posts
    sentiment/<date>.json           aggregated signals
    alerts/<date>.json|.md          actionable alerts
"""

import os
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from core.sport_config import load_sport, available_sports
from core import queries as q
from core import matcher, aggregator, alert_builder, sharp_filter, suggestions
from core import pick_extractor
from adapters.odds import espn, sportsgameodds
from adapters.markets import signals as prediction_markets
from model import ingest as model_ingest
from model import recommend as model_recommend
from adapters.social import reddit, bluesky, twitterapi_io, fourchan, youtube, threads
from adapters.social import telegram_channels as telegram
from adapters.social import timeline_cache
from adapters.social.base import dedupe_posts
from pipeline.cluster import parse_game_time
from pipeline import delivery, news_dedup, grading

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("pipeline")


def team_nickname(full_name: str, team_keywords: dict) -> str:
    """Shortest distinctive keyword for a team (configs list nickname first)."""
    for kw, full in team_keywords.items():
        if full == full_name:
            return kw
    return full_name.split()[-1]


def fetch_odds(cfg, date: str, data_dir: Path) -> dict:
    espn_data = espn.fetch_game_data(cfg, date)
    if not sportsgameodds.enabled():
        return espn_data
    game_data = sportsgameodds.fetch_game_data(cfg, date, debug_dir=data_dir / "debug")
    if not game_data["games"]:
        log.warning("SGO returned no games; falling back to ESPN")
        return espn_data
    # SGO sometimes lists fewer games than ESPN — fill the gaps so the
    # slate stays complete (those games just won't have props)
    missing = [k for k in espn_data["games"] if k not in game_data["games"]]
    for k in missing:
        game_data["games"][k] = espn_data["games"][k]
    if missing:
        log.info(f"filled {len(missing)} game(s) from ESPN missing in SGO")
    return game_data


def fetch_social(cfg, game_data: dict, max_search_queries: int = 30) -> list:
    posts = []
    games = game_data.get("games", {})

    # ── Reddit: general chatter + one targeted search per game ──────────
    if reddit.enabled():
        for sub in cfg.subreddits:
            posts.extend(reddit.fetch_new(sub, limit=50))
        for matchup, g in games.items():
            away_nick = team_nickname(g["away"], cfg.team_keywords)
            home_nick = team_nickname(g["home"], cfg.team_keywords)
            posts.extend(reddit.search(
                f"{away_nick} {home_nick}", cfg.subreddits, limit=15,
                source_game=matchup, source_label="_game_general"))
    else:
        log.warning("Reddit source skipped (no REDDIT_CLIENT_ID/SECRET)")

    # ── Bluesky: targeted searches from generated queries ───────────────
    if bluesky.search_enabled():
        flat = q.flatten_queries(q.build_all_queries(game_data, cfg.stat_aliases))
        # spreads/totals first (broad), then props, capped
        flat.sort(key=lambda x: 0 if x["type"] in ("spread", "total") else 1)
        for item in flat[:max_search_queries]:
            query = item["query"]
            for term in cfg.bluesky_extra_terms:
                query += f" {term}"
            label = item["label"] if item["type"] == "prop" else item["type"]
            posts.extend(bluesky.search(
                query, limit=25, source_game=item["game"], source_label=label))
    else:
        log.warning("Bluesky search skipped (no BLUESKY_HANDLE/APP_PASSWORD)")

    # ── Threads: one keyword search per game (500/7d platform budget) ───
    if threads.enabled():
        for matchup, g in games.items():
            away_nick = team_nickname(g["away"], cfg.team_keywords)
            home_nick = team_nickname(g["home"], cfg.team_keywords)
            posts.extend(threads.search(
                f"{away_nick} {home_nick} bets", limit=25,
                source_game=matchup, source_label="_game_general"))
    else:
        log.info("Threads source skipped (no THREADS_ACCESS_TOKEN)")

    # ── YouTube: one picks-video search per game (comments = public) ────
    if youtube.enabled():
        for matchup, g in games.items():
            away_nick = team_nickname(g["away"], cfg.team_keywords)
            home_nick = team_nickname(g["home"], cfg.team_keywords)
            posts.extend(youtube.search_game(
                f"{away_nick} vs {home_nick} {cfg.display_name} picks prediction",
                source_game=matchup))
    else:
        log.info("YouTube source skipped (no YOUTUBE_API_KEY)")

    # ── 4chan boards: game threads matched by team keywords ─────────────
    if cfg.chan_boards:
        nicknames = set()
        for g in games.values():
            nicknames.add(team_nickname(g["away"], cfg.team_keywords))
            nicknames.add(team_nickname(g["home"], cfg.team_keywords))
        keywords = sorted(nicknames) + [cfg.display_name]
        for board in cfg.chan_boards:
            posts.extend(fourchan.fetch_board(board, keywords))

    # ── Telegram public channels (capper/picks channels) ────────────────
    if cfg.telegram_channels:
        if telegram.enabled():
            posts.extend(telegram.fetch_channels(cfg.telegram_channels))
        else:
            log.warning("Telegram channels configured but source not ready "
                        "(need TELEGRAM_API_ID/HASH + scripts.telegram_login)")

    # ── Twitter (optional, paid): tracked sharp-account timelines.
    # Served through the shared cross-sport cache: same-window runs of
    # different sports pay for Twitter once, and intraday re-fetches pull
    # incrementally (adapters/social/timeline_cache.py).
    if twitterapi_io.enabled():
        tracked = [e.get("handle") for e in cfg.accounts.get("tracked", [])]
        posts.extend(timeline_cache.fetch_timelines(
            tracked, ROOT / "data" / "_shared" / "timeline_cache.json"))
    else:
        log.info("Twitter source skipped (no TWITTERAPI_IO_KEY)")

    return dedupe_posts(posts)


def relabel_market_posts(shaped: dict):
    """Posts labeled 'spread'/'total' by targeted searches belong in the
    game's spread/total buckets, not props."""
    for game_key, game_data in shaped.items():
        if game_key.startswith("_") or not isinstance(game_data, dict):
            continue
        for market in ("spread", "total"):
            hits = game_data.get("props", {}).pop(market, None)
            if hits:
                game_data.setdefault(market, [])
                game_data[market].extend(hits)


def filter_games_to_window(game_data: dict, window_start: str,
                           window_end: str, display_name: str) -> dict:
    """Keep only games tipping inside [window_start, window_end] (local
    ISO, ±15 min tolerance for line-time drift). Games with unparseable
    times are kept rather than silently dropped."""
    ws = datetime.fromisoformat(window_start) - timedelta(minutes=15)
    we = datetime.fromisoformat(window_end) + timedelta(minutes=15)
    keep = {}
    for key, game in game_data["games"].items():
        tip = parse_game_time(game.get("game_time", ""))
        if tip is None or ws <= tip <= we:
            keep[key] = game
    log.info(f"      window {window_start}..{window_end}: "
             f"{len(keep)}/{len(game_data['games'])} {display_name} games")
    game_data["games"] = keep
    return game_data


def run(sport: str, date: str, fmt: str, max_queries: int, notify: bool = True,
        window_start: str = None, window_end: str = None) -> dict:
    cfg = load_sport(sport)
    sharp_filter.load_accounts(cfg.accounts)

    data_dir = ROOT / "data" / sport
    for sub in ("queries", "raw", "sentiment", "alerts", "suggestions", "state"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    # Step 1: odds
    log.info(f"[1/4] Fetching {cfg.display_name} slate for {date}")
    game_data = fetch_odds(cfg, date, data_dir)
    with open(data_dir / "queries" / f"game_data_{date}.json", "w") as f:
        json.dump(game_data, f, indent=2)
    if not game_data["games"]:
        log.warning(f"No {cfg.display_name} games on {date} — nothing to do")
        return {}
    # Cluster runs analyze only their window's games. The full slate was
    # already persisted to queries/ above, so nothing is lost.
    if window_start and window_end:
        game_data = filter_games_to_window(game_data, window_start,
                                           window_end, cfg.display_name)
        if not game_data["games"]:
            log.warning("No games in this run window — nothing to do")
            return {}
    log.info(f"      {len(game_data['games'])} games "
             f"({sum(len(g['props']) for g in game_data['games'].values())} props) "
             f"via {game_data['source']}")

    # Step 1b: prediction-market probabilities (Polymarket + Kalshi, keyless)
    market_signals = prediction_markets.build_signals(
        cfg, date, game_data, data_dir / "state")

    # Step 1c: +EV model recommendations from historical results
    mcfg = cfg.raw.get("model", {})
    history = model_ingest.update_history(
        cfg, data_dir, lookback_days=mcfg.get("history_days", 90),
        end_date=date)
    model_recs = model_recommend.build_recommendations(
        cfg, history, game_data, market_signals, data_dir=data_dir)
    (data_dir / "model").mkdir(parents=True, exist_ok=True)
    with open(data_dir / "model" / f"{date}.json", "w") as f:
        json.dump(model_recs, f, indent=2)

    # Step 2+3: social posts
    log.info("[2/4] Fetching social posts (reddit/bluesky/twitter)")
    posts = fetch_social(cfg, game_data, max_search_queries=max_queries)
    log.info(f"      {len(posts)} unique posts")

    # Step 3b: sharps often post their card as an image/video rather than
    # text — transcribe attached media into post text so matching and
    # sentiment see those picks too.
    pick_extractor.extract_media_picks(posts, data_dir / "state")

    # Step 4a: match posts to games/props
    log.info("[3/4] Matching + aggregating")
    shaped = matcher.shape_posts(posts, game_data["games"],
                                 cfg.team_keywords, cfg.stat_aliases)
    relabel_market_posts(shaped)
    with open(data_dir / "raw" / f"{date}.json", "w") as f:
        json.dump(shaped, f, indent=2)

    sentiment_data = aggregator.run_aggregation(shaped, date)
    with open(data_dir / "sentiment" / f"{date}.json", "w") as f:
        json.dump(sentiment_data, f, indent=2)

    # Step 4b: alerts (news alerts deduped in-run + 3-day cooldown)
    log.info("[4/4] Building alerts")
    alert_data = alert_builder.build_alerts(sentiment_data, cfg.display_name)
    alert_data = news_dedup.filter_news_alerts(alert_data, data_dir / "state")
    with open(data_dir / "alerts" / f"{date}.json", "w") as f:
        json.dump(alert_data, f, indent=2)

    if fmt == "markdown":
        md = alert_builder.format_alerts_markdown(alert_data)
        with open(data_dir / "alerts" / f"{date}.md", "w") as f:
            f.write(md)

    # Step 5: consolidate into betting suggestions + digest
    log.info("[5/5] Building suggestions digest")
    flip_path = data_dir / "state" / "flip_watch.json"
    flip_state = {}
    if flip_path.exists():
        try:
            with open(flip_path) as f:
                flip_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    sug_data = suggestions.build_suggestions(
        game_data, sentiment_data, alert_data, cfg.team_keywords, flip_state,
        market_signals=market_signals, model_recs=model_recs)
    with open(flip_path, "w") as f:
        json.dump(sug_data.pop("new_flip_state"), f, indent=1)
    with open(data_dir / "suggestions" / f"{date}.json", "w") as f:
        json.dump(sug_data, f, indent=2)

    grading_text = grading.grade_previous(cfg, data_dir, date)

    digest = delivery.format_digest(sug_data, cfg.display_name,
                                    grading_text, cfg.team_keywords)
    print(digest)
    if notify:
        delivery.send_text(digest)

    return sug_data


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Multi-sport social sentiment pipeline")
    parser.add_argument("--sport", required=True,
                        help=f"Sport pack to run ({', '.join(available_sports())})")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--format", choices=["console", "markdown"], default="console")
    parser.add_argument("--max-queries", type=int, default=150,
                        help="Cap on targeted social search queries")
    parser.add_argument("--no-notify", action="store_true",
                        help="Skip Telegram delivery of the alert digest")
    parser.add_argument("--window-start",
                        help="Local ISO time: only analyze games tipping "
                             "from here (set by pipeline.dispatch)")
    parser.add_argument("--window-end",
                        help="Local ISO time: only analyze games tipping "
                             "up to here (set by pipeline.dispatch)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.sport, args.date, args.format, args.max_queries,
        notify=not args.no_notify,
        window_start=args.window_start, window_end=args.window_end)


if __name__ == "__main__":
    main()
