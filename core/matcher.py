"""
core/matcher.py
Matches normalized social posts to games and props, producing the
game-keyed structure the aggregator expects:

    {game_key: {"props": {label: [posts]}, "_game_general": [posts]},
     "_general": {"unmatched": [posts]}}

Ported from nba-sentiment scrapers/reshape_tweets.py; team and stat
keywords now come from the sport config instead of hardcoded NBA maps.
"""

import re


def match_post_to_games(text: str, games: dict, team_keywords: dict) -> list:
    """Return game keys whose teams are mentioned in the text."""
    text_lower = text.lower()
    matched_teams = set()
    for keyword, full_name in team_keywords.items():
        if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', text_lower):
            matched_teams.add(full_name)
    matched_games = []
    for game_key in games:
        parts = game_key.split(" @ ")
        if len(parts) == 2:
            away, home = parts
            if away in matched_teams or home in matched_teams:
                matched_games.append(game_key)
    return matched_games


def player_match_terms(player: str) -> list:
    """Forms of the name that count as a mention: the full name and
    'F. Lastname'. Bare surnames are NOT enough — with 35 capper
    timelines in play, matching on 'williams' alone attributed a Gavin
    Williams (pitcher) card and a Courtney Williams (WNBA) parlay to an
    Alika Williams total-bases prop as 'sharp convergence'."""
    name = " ".join(player.split()).lower()
    tokens = name.split()
    terms = [name]
    if len(tokens) >= 2:
        terms.append("{}. {}".format(tokens[0][0], " ".join(tokens[1:])))
    return terms


def match_post_to_props(text: str, player_games: dict, stat_aliases: dict) -> list:
    """Return (game_key, prop_label) pairs for players mentioned in the text."""
    text_lower = " ".join(text.lower().split())
    matches = []
    for player, props in player_games.items():
        if not any(term in text_lower for term in player_match_terms(player)):
            continue
        for prop in props:
            stat = prop["stat"]
            stat_kws = [stat] + list(stat_aliases.get(stat, []))
            if any(kw in text_lower for kw in stat_kws):
                label = "{} {}".format(player, stat)
            else:
                label = "{} general".format(player)
            matches.append((prop["game"], label))
    return matches


def index_player_games(games: dict) -> dict:
    """{player: [{game, stat, line}]} from a game_data games dict."""
    player_games = {}
    for game_key, gdata in games.items():
        for prop in gdata.get("props", []):
            player_games.setdefault(prop["player"], []).append({
                "game": game_key,
                "stat": prop.get("stat", ""),
                "line": prop.get("line", ""),
            })
    return player_games


def shape_posts(posts: list, games: dict, team_keywords: dict,
                stat_aliases: dict) -> dict:
    """Bucket a flat list of normalized posts into the aggregator's shape.

    Posts that carry a source_game (from a targeted search query) are
    trusted to that game; everything else is matched by text content.
    """
    player_games = index_player_games(games)

    keyed: dict = {}
    unmatched: list = []

    def add(game_key, label, post):
        keyed.setdefault(game_key, {"props": {}, "_game_general": []})
        if label == "_game_general":
            keyed[game_key]["_game_general"].append(post)
        else:
            keyed[game_key]["props"].setdefault(label, []).append(post)

    for post in posts:
        text = post.get("text", "")
        matched = False

        # Targeted searches already know their game/prop
        src_game = post.get("source_game")
        src_label = post.get("source_label")
        if src_game and src_game in games:
            add(src_game, src_label or "_game_general", post)
            matched = True

        if not matched:
            for game_key, prop_label in match_post_to_props(text, player_games, stat_aliases):
                add(game_key, prop_label, post)
                matched = True

        if not matched:
            for game_key in match_post_to_games(text, games, team_keywords):
                add(game_key, "_game_general", post)
                matched = True

        if not matched:
            unmatched.append(post)

    keyed["_general"] = {"unmatched": unmatched}
    return keyed
