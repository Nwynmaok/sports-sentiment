# Sports Sentiment Pipeline

Multi-sport port of the nba-sentiment pipeline. Pulls the day's slate and
betting lines, scrapes social chatter about each game/prop, scores sentiment
against sharp/public account classifications, and generates actionable alerts.

The Odds API and Apify dependencies are gone. Sources are now:

| Layer | Primary (free) | Optional (paid/keyed) |
|---|---|---|
| Odds/lines | ESPN hidden API (keyless) | SportsGameOdds (props, free tier key) |
| Social | Reddit (free script app) + Bluesky (free app password) | twitterapi.io (~$0.15/1K tweets) |

Every source sits behind an adapter and degrades gracefully when its
credentials are missing — the pipeline runs with whatever is configured.

## Layout

```
core/                  Sport-agnostic analysis (ported from nba-sentiment)
  sentiment.py         Keyword/pattern sentiment scorer
  sharp_filter.py      Sharp/public/news account classification + divergence
  aggregator.py        Posts -> per-prop signals
  alert_builder.py     Signals -> prioritized alerts (fade/follow/buzz/news)
  matcher.py           Match posts to games/props via team & stat keywords
  queries.py           Slate -> social search queries
  sport_config.py      Sport pack loader
adapters/
  odds/espn.py             Lines + scores, keyless, 20+ leagues
  odds/sportsgameodds.py   Lines + player props (SGO_API_KEY)
  social/reddit.py         OAuth script app, read-only
  social/bluesky.py        Public AppView feeds; search needs app password
  social/twitterapi_io.py  Optional tracked-account timelines + search
sports/<key>/          Sport packs: config.json (+ players/accounts maps)
pipeline/run.py        Orchestrator
```

## Setup

```bash
pip3 install -r requirements.txt
cp .env.example .env   # fill in the free Reddit + Bluesky credentials
```

## Usage

```bash
python3 -m pipeline.run --sport mlb                       # today's slate
python3 -m pipeline.run --sport nba --date 2026-10-25 --format markdown
```

Outputs land under `data/<sport>/{queries,raw,sentiment,alerts}/`.

## Adding a sport

Create `sports/<key>/config.json` with:

- `espn.league_path` — e.g. `"football/nfl"`, `"hockey/nhl"`
- `sportsgameodds.league_id` — e.g. `"NFL"`
- `prop_markets` + `stat_aliases` — the sport's prop language
- `team_keywords` — nickname/abbreviation -> full team name
- `subreddits` — where its betting chatter lives

Optionally add `accounts.json` (tracked sharp/news accounts — Twitter,
Reddit, or Bluesky handles all work) and `players.json` /
`player_team_map.json` for prop targeting. No code changes needed.

## Cron

```cron
0 9 * * * cd /path/to/sports-sentiment && python3 -m pipeline.run --sport mlb --format markdown >> logs/mlb.log 2>&1
```
