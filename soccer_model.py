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


RECENCY_DECAY = 0.93  # each game back in time counts ~7% less than the one after it
PRIOR_STRENGTH = 4.0  # pseudo-matches of league-average form mixed into every team's rate.
                      # Regularizes small samples: a team with a single clean sheet can no longer
                      # produce a 0.0 rating, and one 6-0 blowout can't blow a rate up unboundedly.


def _shrunk_weighted_rates(games, prior_for, prior_against):
    """Recency-weighted goals for/against, shrunk toward the league prior with PRIOR_STRENGTH
    pseudo-matches. games: list of (date, goals_for, goals_against). Returns (gf_rate, ga_rate),
    both strictly positive so they can never zero out an opponent's expected goals."""
    if not games:
        return prior_for, prior_against
    games_sorted = sorted(games, key=lambda g: g[0], reverse=True)
    weight_sum = gf_sum = ga_sum = 0.0
    for i, (_, gf, ga) in enumerate(games_sorted):
        w = RECENCY_DECAY ** i
        weight_sum += w
        gf_sum += w * gf
        ga_sum += w * ga
    gf_rate = (gf_sum + PRIOR_STRENGTH * prior_for) / (weight_sum + PRIOR_STRENGTH)
    ga_rate = (ga_sum + PRIOR_STRENGTH * prior_against) / (weight_sum + PRIOR_STRENGTH)
    return gf_rate, ga_rate


def build_team_stats(matches, pooled=False):
    """Compute recency-weighted, shrinkage-regularized attack/defense ratings per team.

    pooled=False (club leagues): split home and away form, since home advantage is real and
    samples are large (~19 home + 19 away over a season).
    pooled=True (national-team tournaments): combine home and away into one rating set, because
    World Cup / Euro venues are effectively neutral and per-side samples are tiny (1-2 games).
    """
    home_matches = {}  # team -> list of (date, goals_for, goals_against)
    away_matches = {}

    total_home_goals = 0
    total_away_goals = 0
    total_games = 0
    n_home = n_draw = n_away = 0  # outcome counts, for base-rate calibration

    for m in matches:
        score = m.get("score", {}).get("fullTime", {})
        hg, ag = score.get("home"), score.get("away")
        if hg is None or ag is None:
            continue
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        date = m.get("utcDate", "")

        home_matches.setdefault(home, []).append((date, hg, ag))
        away_matches.setdefault(away, []).append((date, ag, hg))

        total_home_goals += hg
        total_away_goals += ag
        total_games += 1
        if hg > ag:
            n_home += 1
        elif hg == ag:
            n_draw += 1
        else:
            n_away += 1

    if total_games == 0:
        return None

    league_avg_home_goals = total_home_goals / total_games
    league_avg_away_goals = total_away_goals / total_games
    league_avg_goals = (total_home_goals + total_away_goals) / (2 * total_games)  # per team, per game
    base_rate = (n_home / total_games, n_draw / total_games, n_away / total_games)

    teams = set(list(home_matches.keys()) + list(away_matches.keys()))
    stats = {}
    for team in teams:
        h_games = home_matches.get(team, [])
        a_games = away_matches.get(team, [])

        if pooled:
            # One rating from all matches, evaluated against the overall (neutral) league average.
            all_games = h_games + a_games
            gf, ga = _shrunk_weighted_rates(all_games, league_avg_goals, league_avg_goals)
            attack = gf / league_avg_goals
            defense = ga / league_avg_goals
            home_attack = away_attack = attack
            home_defense = away_defense = defense
        else:
            h_gf, h_ga = _shrunk_weighted_rates(h_games, league_avg_home_goals, league_avg_away_goals)
            a_gf, a_ga = _shrunk_weighted_rates(a_games, league_avg_away_goals, league_avg_home_goals)
            home_attack = h_gf / league_avg_home_goals
            home_defense = h_ga / league_avg_away_goals
            away_attack = a_gf / league_avg_away_goals
            away_defense = a_ga / league_avg_home_goals

        stats[team] = {
            "home_attack": home_attack,
            "home_defense": home_defense,
            "away_attack": away_attack,
            "away_defense": away_defense,
            "games": len(h_games) + len(a_games),
            "form_ppg": _form_ppg(h_games + a_games),
        }

    return {
        "teams": stats,
        "league_avg_home_goals": league_avg_home_goals,
        "league_avg_away_goals": league_avg_away_goals,
        "elo": compute_elo(matches, pooled),
        "neutral": pooled,
        "base_rate": base_rate,
    }


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


NATIONAL_TEAM_COMPETITIONS = {"WC", "EC"}

# Shrinks the consensus toward the league base rate to fix backtested overconfidence (0 = none, 1 = all base rate).
# Tuned on 4 leagues' 2025 seasons: 0.3 moves tip ROI at fair odds from ~-9% to ~0% (calibrated, not over-
# confident). This makes the displayed probabilities/odds honest; it does NOT manufacture a real-money edge.
CALIBRATION_ALPHA = 0.3

# ---- Equation 2: Dixon-Coles low-score correction to the Poisson grid ----
DC_RHO = -0.10  # negative rho lifts 0-0 / 1-1, trims 1-0 / 0-1: corrects Poisson's under-count of draws


def _dc_tau(x, y, lam, mu, rho=DC_RHO):
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


# ---- Equation 3: Elo ratings (a rating-based paradigm, independent of the goal model) ----
ELO_START = 1500.0
ELO_K = 20.0
ELO_HOME_ADV = 60.0       # rating points of home advantage (0 at neutral venues)
ELO_DRAW_MAX = 0.28       # peak draw probability when teams are evenly rated
ELO_DRAW_SCALE = 300.0


def compute_elo(matches, neutral):
    """Sequential Elo over the match history, with a mild margin-of-victory multiplier."""
    ratings = {}
    home_adv = 0.0 if neutral else ELO_HOME_ADV
    for m in sorted(matches, key=lambda x: x.get("utcDate", "")):
        ft = m.get("score", {}).get("fullTime", {})
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        h = m["homeTeam"]["name"]
        a = m["awayTeam"]["name"]
        rh = ratings.get(h, ELO_START)
        ra = ratings.get(a, ELO_START)
        exp_h = 1.0 / (1.0 + 10 ** (-(rh - ra + home_adv) / 400.0))
        act_h = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        mov = math.log(abs(hg - ag) + 1) + 1.0
        delta = ELO_K * mov * (act_h - exp_h)
        ratings[h] = rh + delta
        ratings[a] = ra - delta
    return ratings


# ---- Equation 4: Recent-form model (points-per-game over the last handful of games) ----
FORM_HOME_ADV_PPG = 0.35  # home teams average ~0.35 more points per game


def _form_ppg(games, max_games=6):
    """Recency-weighted points per game over a team's most recent matches (win=3, draw=1, loss=0)."""
    games_sorted = sorted(games, key=lambda g: g[0], reverse=True)[:max_games]
    if not games_sorted:
        return 1.5  # neutral prior (~mid-table)
    wsum = psum = 0.0
    for i, (_, gf, ga) in enumerate(games_sorted):
        w = RECENCY_DECAY ** i
        pts = 3 if gf > ga else (1 if gf == ga else 0)
        wsum += w
        psum += w * pts
    return psum / wsum


def form_result(league_stats, home_team, away_team):
    """Recent-form win/draw/loss for the home team, from points-per-game momentum."""
    teams = league_stats["teams"]
    hp = teams[home_team].get("form_ppg", 1.5)
    ap = teams[away_team].get("form_ppg", 1.5)
    if not league_stats.get("neutral"):
        hp += FORM_HOME_ADV_PPG
    we = hp / (hp + ap) if (hp + ap) > 0 else 0.5      # expected home points share
    p_draw = 0.30 * math.exp(-(((we - 0.5) / 0.22) ** 2))
    p_home = max(we - p_draw / 2.0, 0.0)
    p_away = max((1.0 - we) - p_draw / 2.0, 0.0)
    s = p_home + p_draw + p_away
    return p_home / s, p_draw / s, p_away / s


def elo_result(league_stats, home_team, away_team):
    """Elo win/draw/loss for the home team."""
    elo = league_stats.get("elo", {})
    rh = elo.get(home_team, ELO_START)
    ra = elo.get(away_team, ELO_START)
    home_adv = 0.0 if league_stats.get("neutral") else ELO_HOME_ADV
    diff = rh - ra + home_adv
    we = 1.0 / (1.0 + 10 ** (-diff / 400.0))             # expected points share for home
    p_draw = ELO_DRAW_MAX * math.exp(-((diff / ELO_DRAW_SCALE) ** 2))
    p_home = max(we - p_draw / 2.0, 0.0)
    p_away = max((1.0 - we) - p_draw / 2.0, 0.0)
    s = p_home + p_draw + p_away
    return p_home / s, p_draw / s, p_away / s


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

    # ---- Equation 1+2: Poisson goal grid with Dixon-Coles low-score correction ----
    grid = {}
    for hg in range(MAX_GOALS + 1):
        for ag in range(MAX_GOALS + 1):
            p = poisson_pmf(hg, exp_home_goals) * poisson_pmf(ag, exp_away_goals)
            p *= _dc_tau(hg, ag, exp_home_goals, exp_away_goals)
            grid[(hg, ag)] = max(p, 0.0)
    gsum = sum(grid.values())
    if gsum == 0:
        return None
    grid = {k: v / gsum for k, v in grid.items()}

    pdc_home = sum(p for (hg, ag), p in grid.items() if hg > ag)
    pdc_draw = sum(p for (hg, ag), p in grid.items() if hg == ag)
    pdc_away = sum(p for (hg, ag), p in grid.items() if hg < ag)

    # ---- Equation 3: Elo result (independent rating-based estimate) ----
    elo_home, elo_draw, elo_away = elo_result(league_stats, home_team, away_team)
    # ---- Equation 4: recent-form result (points-per-game momentum) ----
    form_home, form_draw, form_away = form_result(league_stats, home_team, away_team)

    # ---- Consensus: average the three result models (goals + rating + form) ----
    home_win = (pdc_home + elo_home + form_home) / 3.0
    draw = (pdc_draw + elo_draw + form_draw) / 3.0
    away_win = (pdc_away + elo_away + form_away) / 3.0
    total = home_win + draw + away_win
    home_win, draw, away_win = home_win / total, draw / total, away_win / total

    # ---- Calibration: backtest showed the raw consensus is overconfident, so shrink it toward
    #      the league's empirical base rate. CALIBRATION_ALPHA tuned on historical seasons. ----
    base = league_stats.get("base_rate") or (1 / 3, 1 / 3, 1 / 3)
    a = CALIBRATION_ALPHA
    home_win = (1 - a) * home_win + a * base[0]
    draw = (1 - a) * draw + a * base[1]
    away_win = (1 - a) * away_win + a * base[2]
    total = home_win + draw + away_win
    home_win, draw, away_win = home_win / total, draw / total, away_win / total

    # Goal markets come from the Dixon-Coles grid (Elo doesn't model goals).
    top_scorelines = sorted(grid.items(), key=lambda x: -x[1])[:3]
    over_under = {}
    for line in (1.5, 2.5, 3.5):
        thresh = int(line)  # under 2.5 => total goals <= 2
        p_under = sum(p for (hg, ag), p in grid.items() if hg + ag <= thresh)
        over_under[line] = {"over": 1 - p_under, "under": p_under}
    btts_yes = sum(p for (hg, ag), p in grid.items() if hg > 0 and ag > 0)

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "exp_home_goals": exp_home_goals,
        "exp_away_goals": exp_away_goals,
        "home_attack": home["home_attack"],
        "home_defense": home["home_defense"],
        "away_attack": away["away_attack"],
        "away_defense": away["away_defense"],
        "home_games": home["games"],
        "away_games": away["games"],
        "top_scorelines": top_scorelines,
        "over_under": over_under,
        "btts_yes": btts_yes,
        "models": {
            "poisson_dc": (pdc_home, pdc_draw, pdc_away),
            "elo": (elo_home, elo_draw, elo_away),
            "form": (form_home, form_draw, form_away),
        },
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


MIN_TIP_ODDS = 1.5  # only suggest a tip that pays at least this (i.e. model probability <= 1/1.5 = 66.7%)


def strongest_tip(probs, home_team, away_team, min_odds=MIN_TIP_ODDS):
    """The highest-probability market outcome that is still priced at >= min_odds — i.e. the
    safest bet the model supports that nonetheless pays a meaningful return (avoids suggesting
    near-locks like 'Over 1.5 goals @ 1.07'). Returns (label, probability, fair_odds)."""
    hw, dr, aw = probs["home_win"], probs["draw"], probs["away_win"]
    # Each candidate: (label, probability, market_descriptor). The descriptor lets the results
    # tracker settle the tip against a final score without re-parsing the label text.
    candidates = [
        (f"{home_team} to win", hw, ("HOME",)),
        (f"{away_team} to win", aw, ("AWAY",)),
        ("Draw", dr, ("DRAW",)),
        (f"{home_team} or Draw (double chance)", hw + dr, ("DC", "HD")),
        (f"{away_team} or Draw (double chance)", aw + dr, ("DC", "AD")),
        (f"{home_team} or {away_team} (double chance)", hw + aw, ("DC", "HA")),
        ("Both teams to score: Yes", probs["btts_yes"], ("BTTS", "YES")),
        ("Both teams to score: No", 1 - probs["btts_yes"], ("BTTS", "NO")),
    ]
    for line, d in probs["over_under"].items():
        candidates.append((f"Over {line} goals", d["over"], ("OVER", line)))
        candidates.append((f"Under {line} goals", d["under"], ("UNDER", line)))

    prob_cap = 1.0 / min_odds
    qualifying = [c for c in candidates if 0 < c[1] <= prob_cap]
    # There is always at least one outcome below the cap (the weaker side of the 1X2 trio),
    # but fall back to the full set just in case so we always return something.
    pool = qualifying or candidates

    label, p, descriptor = max(pool, key=lambda c: c[1])
    fair = (1.0 / p) if p > 0 else float("inf")
    return label, p, fair, descriptor


def settle_tip(descriptor, home_goals, away_goals):
    """Evaluate whether a tip's market descriptor hit, given the final score. Returns True/False."""
    kind = descriptor[0]
    total = home_goals + away_goals
    if kind == "HOME":
        return home_goals > away_goals
    if kind == "AWAY":
        return away_goals > home_goals
    if kind == "DRAW":
        return home_goals == away_goals
    if kind == "DC":
        which = descriptor[1]
        if which == "HD":
            return home_goals >= away_goals
        if which == "AD":
            return away_goals >= home_goals
        if which == "HA":
            return home_goals != away_goals
    if kind == "OVER":
        return total > descriptor[1]
    if kind == "UNDER":
        return total < descriptor[1]
    if kind == "BTTS":
        both = home_goals > 0 and away_goals > 0
        return both if descriptor[1] == "YES" else not both
    return False


def format_match_probabilities(competition_name, home_team, away_team, probs, kickoff, sample_note="", neutral=False):
    home_attack_phrase = _rating_phrase(probs["home_attack"], "attack")
    home_defense_phrase = _rating_phrase(probs["home_defense"], "defense")
    away_attack_phrase = _rating_phrase(probs["away_attack"], "attack")
    away_defense_phrase = _rating_phrase(probs["away_defense"], "defense")
    home_ctx = "overall" if neutral else "at home"
    away_ctx = "overall" if neutral else "away"

    scorelines_str = ", ".join(
        f"{hg}-{ag} ({p*100:.1f}%)" for (hg, ag), p in probs["top_scorelines"]
    )
    ou = probs["over_under"]
    ou_str = "  |  ".join(
        f"O/U {line}: {ou[line]['over']*100:.1f}% over / {ou[line]['under']*100:.1f}% under"
        for line in sorted(ou.keys())
    )

    tip_label, tip_prob, tip_fair, _tip_desc = strongest_tip(probs, home_team, away_team)

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
        f"  • {home_team} {home_ctx}: {home_attack_phrase}; {home_defense_phrase}.\n"
        f"  • {away_team} {away_ctx}: {away_attack_phrase}; {away_defense_phrase}.\n"
        f"  • Expected goals come from multiplying each team's scoring rate by the opponent's conceding rate, "
        f"relative to the league's average goal totals. Scorelines, over/under, and BTTS are all "
        f"derived from that same expected-goals estimate.\n"
        f"\n"
        f"  📊 **Statistically Strongest Tip:** {tip_label} — model probability **{tip_prob*100:.1f}%** "
        f"(fair odds {tip_fair:.2f}). This is the highest-probability outcome the model supports that still pays "
        f"at least 1.50; it's a probability, not a certainty — only worth backing if a sportsbook prices it above {tip_fair:.2f}.\n"
        f"  _Poisson model from each team's actual goals scored/conceded.{sample_note} Statistical estimate, not a guaranteed outcome._"
    )


def _confidence_color(top_prob):
    """Accent color by how strong the model's lean is: green = strong, blue = moderate, grey = toss-up."""
    if top_prob >= 0.55:
        return 0x2ECC71  # green
    if top_prob >= 0.45:
        return 0x3498DB  # blue
    return 0x95A5A6      # grey


def format_match_embed(competition_name, home_team, away_team, probs, kickoff,
                       sample_note="", neutral=False, emblem_url=None, include_tip=True, premium=False):
    """Build a rich Discord embed (color-coded, structured fields, competition emblem) for a match.
    include_tip=False hides the actual pick behind a VIP teaser (free-tier version).
    premium=True flags the match as a high-confidence Gold 'premium play'."""
    home_ctx = "overall" if neutral else "at home"
    away_ctx = "overall" if neutral else "away"

    hw, dr, aw = probs["home_win"], probs["draw"], probs["away_win"]
    top_prob = max(hw, dr, aw)
    tip_label, tip_prob, tip_fair, _tip_desc = strongest_tip(probs, home_team, away_team)
    ou = probs["over_under"]

    # Discord auto-localizes <t:unix:F> to each viewer's own timezone.
    kickoff_line = kickoff
    try:
        from datetime import datetime as _dt
        ts = int(_dt.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp())
        kickoff_line = f"<t:{ts}:F>  (<t:{ts}:R>)"
    except (ValueError, AttributeError):
        pass

    result_field = (
        f"🏠 **{home_team}**  `{hw*100:.1f}%`\n"
        f"🤝 Draw  `{dr*100:.1f}%`\n"
        f"🛫 **{away_team}**  `{aw*100:.1f}%`\n"
        f"_consensus of {len(probs.get('models', {})) or 1} models_"
    )
    models = probs.get("models", {})
    methods_field = None
    if models:
        pdc = models.get("poisson_dc")
        elo = models.get("elo")
        form = models.get("form")
        methods_field = (
            f"`Poisson+DC`  {pdc[0]*100:.0f}/{pdc[1]*100:.0f}/{pdc[2]*100:.0f}\n"
            f"`Elo rating`  {elo[0]*100:.0f}/{elo[1]*100:.0f}/{elo[2]*100:.0f}\n"
            f"`Form (PPG)`  {form[0]*100:.0f}/{form[1]*100:.0f}/{form[2]*100:.0f}\n"
            f"`Consensus `  {hw*100:.0f}/{dr*100:.0f}/{aw*100:.0f}\n"
            f"_home / draw / away %_"
        )
    goals_field = (
        f"Over 1.5  `{ou[1.5]['over']*100:.0f}%`\n"
        f"Over 2.5  `{ou[2.5]['over']*100:.0f}%`\n"
        f"Over 3.5  `{ou[3.5]['over']*100:.0f}%`\n"
        f"BTTS  `{probs['btts_yes']*100:.0f}% yes`"
    )
    scores_field = "\n".join(f"`{hg}-{ag}`  {p*100:.1f}%" for (hg, ag), p in probs["top_scorelines"])

    if include_tip:
        why_field = (
            f"_Sample: {home_team} {probs['home_games']} games, {away_team} {probs['away_games']} games_\n"
            f"• **{home_team}** {home_ctx}: {_rating_phrase(probs['home_attack'],'attack')}; "
            f"{_rating_phrase(probs['home_defense'],'defense')}.\n"
            f"• **{away_team}** {away_ctx}: {_rating_phrase(probs['away_attack'],'attack')}; "
            f"{_rating_phrase(probs['away_defense'],'defense')}."
        )
    else:
        why_field = (
            "🔒 **VIP only** — the model's full reasoning (each team's attack/defense form and "
            "how the projection is built) is reserved for **Silver** and **Gold** members."
        )
    if include_tip:
        tip_field = (
            f"➡️ **{tip_label}**\n"
            f"Model probability `{tip_prob*100:.1f}%`  •  Fair odds `{tip_fair:.2f}`\n"
            f"_Only worth backing if a book prices it above {tip_fair:.2f}._"
        )
    else:
        tip_field = (
            "🔒 **VIP only** — the model's recommended bet for this match is reserved for "
            "**Silver** and **Gold** members. Upgrade to unlock every pick."
        )

    title = f"⚽ {home_team}  vs  {away_team}"
    if premium:
        title = "⭐ PREMIUM PLAY  •  " + title

    embed = {
        "title": title,
        "description": f"🕐 Kickoff: {kickoff_line}\n📐 Expected score: **{probs['exp_home_goals']:.1f} – {probs['exp_away_goals']:.1f}**",
        "color": 0xF1C40F if premium else _confidence_color(top_prob),
        "author": {"name": competition_name},
        "fields": [
            {"name": "📊 Match Result", "value": result_field, "inline": True},
            {"name": "🥅 Goals", "value": goals_field, "inline": True},
            {"name": "🎯 Likely Scores", "value": scores_field, "inline": True},
        ]
        + ([{"name": "🧮 Models", "value": methods_field, "inline": False}] if methods_field else [])
        + [
            {"name": "📈 Statistically Strongest Tip", "value": tip_field, "inline": False},
            {"name": "🧠 Why", "value": why_field, "inline": False},
        ],
        "footer": {"text": f"Ensemble: Poisson + Dixon-Coles + Elo + recent form.{sample_note} Statistical estimate, not a guaranteed outcome — bet responsibly."},
    }
    if emblem_url:
        embed["author"]["icon_url"] = emblem_url
        embed["thumbnail"] = {"url": emblem_url}
    return embed


def scan_competition(competition_code, competition_name):
    """Returns a list of per-match dicts for upcoming matches in this competition. Each dict has:
    probs, embed (ready to post), kickoff, match_id, home, away, competition, tip_label,
    tip_odds, tip_descriptor (for later settlement)."""
    national = competition_code in NATIONAL_TEAM_COMPETITIONS
    finished = get_finished_matches(competition_code)
    league_stats = build_team_stats(finished, pooled=national)
    if not league_stats:
        return []

    min_games = 2 if national else 6
    sample_note = (
        " Based on a small early-tournament sample (heavily regularized toward the field average)."
        if national else ""
    )

    upcoming = get_scheduled_matches(competition_code)
    results = []
    for m in upcoming:
        home_team = m["homeTeam"]["name"]
        away_team = m["awayTeam"]["name"]
        probs = match_probabilities(league_stats, home_team, away_team, min_games=min_games)
        if not probs:
            continue
        kickoff = m.get("utcDate", "TBD")
        emblem = (m.get("competition") or {}).get("emblem")
        tip_label, tip_prob, tip_odds, tip_desc = strongest_tip(probs, home_team, away_team)
        results.append({
            "probs": probs,
            "kickoff": kickoff,
            "match_id": m["id"],
            "home": home_team,
            "away": away_team,
            "competition": competition_name,
            "sample_note": sample_note,
            "neutral": national,
            "emblem": emblem,
            "tip_label": tip_label,
            "tip_prob": tip_prob,
            "tip_odds": tip_odds,
            "tip_descriptor": tip_desc,
        })
    return results


def embed_for(match, include_tip=True, premium=False):
    """Render a Discord embed for a match dict from scan_competition, with the given access flags."""
    return format_match_embed(
        match["competition"], match["home"], match["away"], match["probs"], match["kickoff"],
        sample_note=match["sample_note"], neutral=match["neutral"], emblem_url=match["emblem"],
        include_tip=include_tip, premium=premium,
    )


def get_match_result(competition_code, match_id):
    """Return (home_goals, away_goals) for a finished match, settled on REGULAR TIME (90').
    Football-data's `fullTime` includes extra time and penalty shootouts for knockout games, but
    betting markets settle on the 90-minute score — so for any match that went to ET/penalties we
    use `regularTime` instead. Returns None if not finished/found."""
    finished = get_finished_matches(competition_code)
    for m in finished:
        if m["id"] == match_id:
            score = m.get("score", {})
            if score.get("duration") in ("EXTRA_TIME", "PENALTY_SHOOTOUT") and score.get("regularTime"):
                rt = score["regularTime"]
                hg, ag = rt.get("home"), rt.get("away")
            else:
                ft = score.get("fullTime", {})
                hg, ag = ft.get("home"), ft.get("away")
            if hg is not None and ag is not None:
                return hg, ag
    return None
