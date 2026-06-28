import math
import time
import requests

FD_API_KEY = None  # set by caller
FD_BASE = "https://api.football-data.org/v4"

# Free-tier competitions: major club leagues + Champions League + national team tournaments
COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "WC": "World Cup",
    "EC": "European Championship",
}

MAX_GOALS = 8  # grid size for Poisson outcome summation
LOOKBACK_MATCHES = 38  # roughly one season


def fd_get(path, params=None):
    headers = {"X-Auth-Token": FD_API_KEY}
    resp = requests.get(f"{FD_BASE}{path}", headers=headers, params=params or {}, timeout=20)
    if resp.status_code == 429:
        time.sleep(60)
        resp = requests.get(f"{FD_BASE}{path}", headers=headers, params=params or {}, timeout=20)
    if resp.status_code != 200:
        print(f"football-data.org error {resp.status_code} for {path}: {resp.text[:200]}")
        return None
    return resp.json()


def get_finished_matches(competition_code):
    """Pull finished matches, falling back to the previous season if the current one
    has no results yet (e.g. a club league in its off-season)."""
    data = fd_get(f"/competitions/{competition_code}/matches", {"status": "FINISHED"})
    matches = data.get("matches", []) if data else []
    if matches:
        return matches

    from datetime import datetime as _dt
    current_year = _dt.utcnow().year
    for season in (current_year - 1, current_year - 2):
        data = fd_get(f"/competitions/{competition_code}/matches", {"status": "FINISHED", "season": season})
        matches = data.get("matches", []) if data else []
        if matches:
            return matches
    return []


def get_scheduled_matches(competition_code, limit_days=7):
    data = fd_get(f"/competitions/{competition_code}/matches", {"status": "SCHEDULED"})
    if not data:
        return []
    return data.get("matches", [])[:30]


def build_team_stats(matches):
    """Compute average goals scored/conceded home & away per team, plus league-wide averages."""
    home_goals_for = {}
    home_goals_against = {}
    home_games = {}
    away_goals_for = {}
    away_goals_against = {}
    away_games = {}

    total_home_goals = 0
    total_away_goals = 0
    total_games = 0

    for m in matches:
        score = m.get("score", {}).get("fullTime", {})
        hg, ag = score.get("home"), score.get("away")
        if hg is None or ag is None:
            continue
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]

        home_goals_for[home] = home_goals_for.get(home, 0) + hg
        home_goals_against[home] = home_goals_against.get(home, 0) + ag
        home_games[home] = home_games.get(home, 0) + 1

        away_goals_for[away] = away_goals_for.get(away, 0) + ag
        away_goals_against[away] = away_goals_against.get(away, 0) + hg
        away_games[away] = away_games.get(away, 0) + 1

        total_home_goals += hg
        total_away_goals += ag
        total_games += 1

    if total_games == 0:
        return None

    league_avg_home_goals = total_home_goals / total_games
    league_avg_away_goals = total_away_goals / total_games

    teams = set(list(home_games.keys()) + list(away_games.keys()))
    stats = {}
    for team in teams:
        hg_games = home_games.get(team, 0)
        ag_games = away_games.get(team, 0)
        home_attack = (home_goals_for.get(team, 0) / hg_games / league_avg_home_goals) if hg_games else 1.0
        home_defense = (home_goals_against.get(team, 0) / hg_games / league_avg_away_goals) if hg_games else 1.0
        away_attack = (away_goals_for.get(team, 0) / ag_games / league_avg_away_goals) if ag_games else 1.0
        away_defense = (away_goals_against.get(team, 0) / ag_games / league_avg_home_goals) if ag_games else 1.0
        stats[team] = {
            "home_attack": home_attack,
            "home_defense": home_defense,
            "away_attack": away_attack,
            "away_defense": away_defense,
            "games": hg_games + ag_games,
        }

    return {
        "teams": stats,
        "league_avg_home_goals": league_avg_home_goals,
        "league_avg_away_goals": league_avg_away_goals,
    }


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


NATIONAL_TEAM_COMPETITIONS = {"WC", "EC"}


def match_probabilities(league_stats, home_team, away_team, min_games=6):
    teams = league_stats["teams"]
    if home_team not in teams or away_team not in teams:
        return None
    home = teams[home_team]
    away = teams[away_team]

    # Minimum sample size to trust the team's own rates; otherwise this match is too uncertain to call.
    if home["games"] < min_games or away["games"] < min_games:
        return None

    exp_home_goals = league_stats["league_avg_home_goals"] * home["home_attack"] * away["away_defense"]
    exp_away_goals = league_stats["league_avg_away_goals"] * away["away_attack"] * home["home_defense"]

    home_win = draw = away_win = 0.0
    for hg in range(MAX_GOALS + 1):
        for ag in range(MAX_GOALS + 1):
            p = poisson_pmf(hg, exp_home_goals) * poisson_pmf(ag, exp_away_goals)
            if hg > ag:
                home_win += p
            elif hg == ag:
                draw += p
            else:
                away_win += p

    total = home_win + draw + away_win
    if total == 0:
        return None

    # Exact scorelines: same independent-Poisson grid, just kept per-cell instead of collapsed to W/D/L.
    scoreline_probs = {}
    for hg in range(MAX_GOALS + 1):
        for ag in range(MAX_GOALS + 1):
            scoreline_probs[(hg, ag)] = poisson_pmf(hg, exp_home_goals) * poisson_pmf(ag, exp_away_goals)
    top_scorelines = sorted(scoreline_probs.items(), key=lambda x: -x[1])[:3]

    # Total goals = sum of two independent Poisson variables -> itself Poisson(lambda_home + lambda_away).
    total_lambda = exp_home_goals + exp_away_goals
    over_under = {}
    for line in (1.5, 2.5, 3.5):
        threshold = int(line) + 1  # over 2.5 means >=3 goals
        p_under = sum(poisson_pmf(k, total_lambda) for k in range(threshold))
        over_under[line] = {"over": 1 - p_under, "under": p_under}

    # BTTS: independent home/away scoring, so P(both score) = (1 - P(home=0)) * (1 - P(away=0)).
    p_home_scores = 1 - poisson_pmf(0, exp_home_goals)
    p_away_scores = 1 - poisson_pmf(0, exp_away_goals)
    btts_yes = p_home_scores * p_away_scores

    return {
        "home_win": home_win / total,
        "draw": draw / total,
        "away_win": away_win / total,
        "exp_home_goals": exp_home_goals,
        "exp_away_goals": exp_away_goals,
        "home_attack": home["home_attack"],
        "home_defense": home["home_defense"],
        "away_attack": away["away_attack"],
        "away_defense": away["away_defense"],
        "home_games": home["games"],
        "away_games": away["games"],
        "top_scorelines": [(score, p / total) for score, p in top_scorelines],
        "over_under": over_under,
        "btts_yes": btts_yes,
    }


def _rating_phrase(value, kind):
    """kind is 'attack' or 'defense'. For defense, lower value = better (concedes less)."""
    pct = (value - 1.0) * 100
    if kind == "attack":
        if pct >= 5:
            return f"scores {pct:.0f}% more than the league average"
        elif pct <= -5:
            return f"scores {abs(pct):.0f}% less than the league average"
        return "scores about league-average"
    else:
        if pct >= 5:
            return f"concedes {pct:.0f}% more than the league average (weaker defense)"
        elif pct <= -5:
            return f"concedes {abs(pct):.0f}% less than the league average (stronger defense)"
        return "concedes about league-average"


def format_match_probabilities(competition_name, home_team, away_team, probs, kickoff, sample_note=""):
    home_attack_phrase = _rating_phrase(probs["home_attack"], "attack")
    home_defense_phrase = _rating_phrase(probs["home_defense"], "defense")
    away_attack_phrase = _rating_phrase(probs["away_attack"], "attack")
    away_defense_phrase = _rating_phrase(probs["away_defense"], "defense")

    scorelines_str = ", ".join(
        f"{hg}-{ag} ({p*100:.1f}%)" for (hg, ag), p in probs["top_scorelines"]
    )
    ou = probs["over_under"]
    ou_str = "  |  ".join(
        f"O/U {line}: {ou[line]['over']*100:.1f}% over / {ou[line]['under']*100:.1f}% under"
        for line in sorted(ou.keys())
    )

    return (
        f"**[{competition_name}] {home_team} vs {away_team}**\n"
        f"  Kickoff: {kickoff}\n"
        f"  • {home_team} win: {probs['home_win']*100:.1f}%\n"
        f"  • Draw: {probs['draw']*100:.1f}%\n"
        f"  • {away_team} win: {probs['away_win']*100:.1f}%\n"
        f"  Expected score: {probs['exp_home_goals']:.1f} - {probs['exp_away_goals']:.1f}\n"
        f"  Most likely scorelines: {scorelines_str}\n"
        f"  {ou_str}\n"
        f"  Both teams to score: {probs['btts_yes']*100:.1f}% yes / {(1-probs['btts_yes'])*100:.1f}% no\n"
        f"\n"
        f"  **Why:** (sample: {home_team} {probs['home_games']} games, {away_team} {probs['away_games']} games)\n"
        f"  • {home_team} at home: {home_attack_phrase}; {home_defense_phrase}.\n"
        f"  • {away_team} away: {away_attack_phrase}; {away_defense_phrase}.\n"
        f"  • Expected goals come from multiplying each team's scoring rate by the opponent's conceding rate, "
        f"relative to the league's average home/away goal totals. Scorelines, over/under, and BTTS are all "
        f"derived from that same expected-goals estimate.\n"
        f"  _Poisson model from each team's actual goals scored/conceded.{sample_note} Statistical estimate, not a guaranteed outcome._"
    )


def scan_competition(competition_code, competition_name):
    """Returns list of (probs, message, kickoff_iso, match_id) for upcoming matches in this competition."""
    finished = get_finished_matches(competition_code)
    league_stats = build_team_stats(finished)
    if not league_stats:
        return []

    min_games = 2 if competition_code in NATIONAL_TEAM_COMPETITIONS else 6
    sample_note = " Based on a small early-tournament sample." if competition_code in NATIONAL_TEAM_COMPETITIONS else ""

    upcoming = get_scheduled_matches(competition_code)
    results = []
    for m in upcoming:
        home_team = m["homeTeam"]["name"]
        away_team = m["awayTeam"]["name"]
        probs = match_probabilities(league_stats, home_team, away_team, min_games=min_games)
        if not probs:
            continue
        kickoff = m.get("utcDate", "TBD")
        msg = format_match_probabilities(competition_name, home_team, away_team, probs, kickoff, sample_note)
        results.append((probs, msg, kickoff, m["id"]))
    return results
