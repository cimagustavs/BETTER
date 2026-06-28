import os
import time
import requests
from itertools import combinations
from datetime import datetime, timezone

import soccer_model

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
ARB_WEBHOOK = os.environ["ARB_WEBHOOK"]
VAL_WEBHOOK = os.environ["VAL_WEBHOOK"]
FREE_WEBHOOK = os.environ["FREE_WEBHOOK"]
SILVER_WEBHOOK = os.environ["SILVER_WEBHOOK"]
GOLD_WEBHOOK = os.environ["GOLD_WEBHOOK"]

soccer_model.FD_API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]
SOCCER_DIGEST_INTERVAL_SECONDS = 12 * 3600  # football-data free tier is rate-limited; twice a day is plenty

SPORTS = ["americanfootball_nfl", "basketball_nba", "baseball_mlb", "icehockey_nhl", "soccer_epl"]
REGIONS = "us"
MARKETS = "h2h"
EV_THRESHOLD = 0.03  # 3% min edge to post a value bet
SCAN_INTERVAL_SECONDS = 1800  # 30 minutes
DAILY_DIGEST_INTERVAL_SECONDS = 24 * 3600

SHARP_BOOKS = {"pinnacle", "betonlineag", "lowvig"}

posted_signals = set()
last_digest_at = 0
last_soccer_digest_at = 0


def fetch_odds(sport):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"[{sport}] odds fetch failed: {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def implied_prob(decimal_odds):
    return 1.0 / decimal_odds


def no_vig_fair_prob(outcome_probs):
    total = sum(outcome_probs)
    return [p / total for p in outcome_probs]


def find_arbitrage(event):
    outcomes = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                name = outcome["name"]
                price = outcome["price"]
                if name not in outcomes or price > outcomes[name]["price"]:
                    outcomes[name] = {"price": price, "book": bookmaker["title"]}

    if len(outcomes) < 2:
        return None

    total_implied = sum(implied_prob(o["price"]) for o in outcomes.values())
    if total_implied < 1.0:
        margin = (1.0 - total_implied) * 100
        return {"outcomes": outcomes, "margin": margin}
    return None


def find_value_bets(event):
    signals = []
    sharp_probs = {}
    sharp_count = 0
    for bookmaker in event.get("bookmakers", []):
        if bookmaker["key"] not in SHARP_BOOKS:
            continue
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            sharp_count += 1
            raw_probs = [implied_prob(o["price"]) for o in market["outcomes"]]
            fair_probs = no_vig_fair_prob(raw_probs)
            for outcome, fair_p in zip(market["outcomes"], fair_probs):
                sharp_probs[outcome["name"]] = max(sharp_probs.get(outcome["name"], 0), fair_p)

    if not sharp_probs:
        return signals

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                name = outcome["name"]
                price = outcome["price"]
                fair_p = sharp_probs.get(name)
                if fair_p is None:
                    continue
                offered_p = implied_prob(price)
                edge = fair_p - offered_p
                if edge >= EV_THRESHOLD:
                    signals.append({
                        "outcome": name,
                        "book": bookmaker["title"],
                        "price": price,
                        "fair_prob": fair_p,
                        "edge": edge,
                    })
    return signals


def fair_probabilities(event):
    """Average no-vig fair probability per outcome across all books offering h2h."""
    sums = {}
    counts = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            raw_probs = [implied_prob(o["price"]) for o in market["outcomes"]]
            fair_probs = no_vig_fair_prob(raw_probs)
            for outcome, fair_p in zip(market["outcomes"], fair_probs):
                sums[outcome["name"]] = sums.get(outcome["name"], 0) + fair_p
                counts[outcome["name"]] = counts.get(outcome["name"], 0) + 1
    if not sums:
        return None
    return {name: sums[name] / counts[name] for name in sums}


def format_percentages(event, probs):
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    lines = [f"**{away} @ {home}**"]
    for name, p in sorted(probs.items(), key=lambda x: -x[1]):
        lines.append(f"  • {name}: {p*100:.1f}% fair win probability")
    return "\n".join(lines)


def post_to_discord(webhook_url, content):
    for attempt in range(3):
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.2)
            continue
        print(f"Discord post failed: {resp.status_code} {resp.text[:200]}")
        return
    print(f"Discord post gave up after rate limiting: {content[:60]}")


def format_arbitrage(event, arb):
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    lines = [f"**ARBITRAGE: {away} @ {home}**", f"Guaranteed margin: {arb['margin']:.2f}%"]
    for name, info in arb["outcomes"].items():
        lines.append(f"  • {name}: {info['price']} ({info['book']})")
    lines.append("_Stake split across these books locks in profit regardless of outcome._")
    return "\n".join(lines)


def format_value_bet(event, sig):
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    return (
        f"**VALUE BET: {away} @ {home}**\n"
        f"  • Pick: {sig['outcome']} @ {sig['price']} ({sig['book']})\n"
        f"  • Fair (no-vig sharp) win probability: {sig['fair_prob']*100:.1f}%\n"
        f"  • Edge vs. offered odds: {sig['edge']*100:.1f}%\n"
        f"_Statistical edge based on live odds data, not a guaranteed outcome. Bet responsibly._"
    )


def run_scan():
    for sport in SPORTS:
        events = fetch_odds(sport)
        for event in events:
            event_id = event.get("id")

            arb = find_arbitrage(event)
            if arb:
                key = f"arb:{event_id}:{round(arb['margin'], 1)}"
                if key not in posted_signals:
                    post_to_discord(ARB_WEBHOOK, format_arbitrage(event, arb))
                    post_to_discord(GOLD_WEBHOOK, format_arbitrage(event, arb))
                    posted_signals.add(key)

            for sig in find_value_bets(event):
                key = f"val:{event_id}:{sig['outcome']}:{sig['book']}:{round(sig['edge'],2)}"
                if key not in posted_signals:
                    post_to_discord(VAL_WEBHOOK, format_value_bet(event, sig))
                    if sig["edge"] >= 0.05:
                        post_to_discord(GOLD_WEBHOOK, format_value_bet(event, sig))
                    else:
                        post_to_discord(SILVER_WEBHOOK, format_value_bet(event, sig))
                    posted_signals.add(key)

        time.sleep(1)


def run_daily_digest():
    """Post fair win-probability breakdown for every match today, to Free/Silver/Gold."""
    header = f"**Daily Fair-Probability Digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}**\n_Derived mathematically from live no-vig odds across books. Not a prediction of outcome._\n"
    post_to_discord(FREE_WEBHOOK, header)
    post_to_discord(SILVER_WEBHOOK, header)
    post_to_discord(GOLD_WEBHOOK, header)

    for sport in SPORTS:
        events = fetch_odds(sport)
        for event in events:
            probs = fair_probabilities(event)
            if not probs:
                continue
            msg = format_percentages(event, probs)
            post_to_discord(FREE_WEBHOOK, msg)
            post_to_discord(SILVER_WEBHOOK, msg)
            post_to_discord(GOLD_WEBHOOK, msg)
        time.sleep(1)


def run_soccer_digest():
    """Primary signal: Poisson-model win/draw/loss probabilities for upcoming soccer matches,
    built from each team's actual historical goals scored/conceded. Routed by how lopsided the
    model's probability spread is (a statistical confidence proxy, not a prediction of outcome)."""
    header = (
        f"**Soccer Match Probabilities — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}**\n"
        f"_Poisson model from real historical results (goals scored/conceded, home/away splits). "
        f"Statistical estimate only — not a guaranteed outcome._\n"
    )
    posted_any = False
    for code, name in soccer_model.COMPETITIONS.items():
        try:
            results = soccer_model.scan_competition(code, name)
        except Exception as e:
            print(f"Soccer scan failed for {name}: {e}")
            continue
        for probs, msg in results:
            top_prob = max(probs["home_win"], probs["draw"], probs["away_win"])
            if not posted_any:
                post_to_discord(FREE_WEBHOOK, header)
                post_to_discord(SILVER_WEBHOOK, header)
                post_to_discord(GOLD_WEBHOOK, header)
                posted_any = True

            post_to_discord(FREE_WEBHOOK, msg)
            if top_prob >= 0.45:
                post_to_discord(SILVER_WEBHOOK, msg)
            if top_prob >= 0.55:
                post_to_discord(GOLD_WEBHOOK, msg)
        time.sleep(2)


def main():
    global last_digest_at, last_soccer_digest_at
    print(f"Signal bot starting at {datetime.now(timezone.utc).isoformat()}")
    while True:
        try:
            run_scan()
            now = time.time()
            if now - last_digest_at >= DAILY_DIGEST_INTERVAL_SECONDS:
                run_daily_digest()
                last_digest_at = now
            if now - last_soccer_digest_at >= SOCCER_DIGEST_INTERVAL_SECONDS:
                run_soccer_digest()
                last_soccer_digest_at = now
        except Exception as e:
            print(f"Scan error: {e}")
        if len(posted_signals) > 5000:
            posted_signals.clear()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
