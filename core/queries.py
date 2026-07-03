"""
core/queries.py
Generates social search queries from game lines / props data.
Ported from nba-sentiment analysis/search_queries.py; stat aliases and
extra templates now come from the sport config.
"""

import json
import argparse
from datetime import datetime
from pathlib import Path

PROP_TEMPLATES = [
    "{player} {stat} over",
    "{player} {stat} under",
    "{player} {stat} o{line}",
    "{player} {stat} u{line}",
    "{player} prop",
]

SPREAD_TEMPLATES = [
    "{team} {spread}",
    "{team} spread",
    "{away} vs {home}",
]

TOTAL_TEMPLATES = [
    "{away} {home} over",
    "{away} {home} under",
    "{away} vs {home} total",
]


def normalise_stat(raw: str, stat_aliases: dict) -> str:
    raw_lower = raw.lower()
    for canonical, aliases in stat_aliases.items():
        if raw_lower == canonical or raw_lower in aliases:
            return canonical
    return raw_lower


def generate_prop_queries(player: str, stat: str, line: str = "") -> list:
    queries = []
    for tmpl in PROP_TEMPLATES:
        q = tmpl.format(player=player, stat=stat, line=line).strip()
        queries.append(q)
    return list(dict.fromkeys(queries))


def generate_spread_queries(home: str, away: str, spread_line: str = "") -> list:
    queries = []
    for tmpl in SPREAD_TEMPLATES:
        queries.append(tmpl.format(team=home, spread=spread_line, home=home, away=away).strip())
        queries.append(tmpl.format(team=away, spread=spread_line, home=home, away=away).strip())
    return list(dict.fromkeys(queries))


def generate_total_queries(home: str, away: str) -> list:
    return list(dict.fromkeys(
        tmpl.format(home=home, away=away).strip() for tmpl in TOTAL_TEMPLATES
    ))


def build_all_queries(data: dict, stat_aliases: dict = None) -> dict:
    """
    Input: normalised game data dict ({games: {matchup: {props, spread, total}}})
    Output: {game_key: {props: {label: [q...]}, spreads: [...], totals: [...]}}
    """
    stat_aliases = stat_aliases or {}
    result = {}
    games = data.get("games", {})

    for game_key, game_data in games.items():
        result[game_key] = {"props": {}, "spreads": [], "totals": []}

        for prop in game_data.get("props", []):
            player = prop["player"]
            stat = normalise_stat(prop.get("stat", ""), stat_aliases)
            line = str(prop.get("line", ""))
            result[game_key]["props"][f"{player} {stat}"] = generate_prop_queries(player, stat, line)

        spread = game_data.get("spread")
        if spread:
            result[game_key]["spreads"] = generate_spread_queries(
                spread.get("home", ""), spread.get("away", ""), str(spread.get("line", "")))
        elif "@" in game_key:
            away, home = [s.strip() for s in game_key.split("@", 1)]
            result[game_key]["spreads"] = generate_spread_queries(home, away)

        if "@" in game_key:
            away, home = [s.strip() for s in game_key.split("@", 1)]
            result[game_key]["totals"] = generate_total_queries(home, away)

    return result


def flatten_queries(query_map: dict) -> list:
    """Returns a flat list of {game, type, label, query} dicts for the scrapers."""
    flat = []
    for game, data in query_map.items():
        for prop_label, queries in data["props"].items():
            for q in queries:
                flat.append({"game": game, "type": "prop", "label": prop_label, "query": q})
        for q in data["spreads"]:
            flat.append({"game": game, "type": "spread", "label": game, "query": q})
        for q in data["totals"]:
            flat.append({"game": game, "type": "total", "label": game, "query": q})
    return flat


def main():
    parser = argparse.ArgumentParser(description="Generate search queries from game lines")
    parser.add_argument("--games-file", required=True)
    parser.add_argument("--output", default="queries.json")
    parser.add_argument("--flat", action="store_true")
    args = parser.parse_args()

    with open(args.games_file) as f:
        data = json.load(f)

    query_map = build_all_queries(data)
    output = flatten_queries(query_map) if args.flat else query_map

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"[queries] Generated {len(flatten_queries(query_map))} queries -> {out_path}")


if __name__ == "__main__":
    main()
