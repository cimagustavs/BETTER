import os
import time
import requests
from itertools import combinations
from datetime import datetime, timezone

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
ARB_WEBHOOK = os.environ["ARB_WEBHOOK"]
VAL_WEBHOOK = os.environ["VAL_WEBHOOK"]

SPORTS = ["americanfootball_nfl", "basketball_nba", "baseball_mlb", "icehockey_nhl", "soccer_epl"]
REGIONS = "us"
MARKETS = "h2h"
EV_THRESHOLD = 0.03  # 3% min edge to post a value bet
SCAN_INTERVAL_SECONDS = 1800  # 30 minutes

SHARP_BOOKS = {"pinnacle", "betonlineag", "lowvig"}

posted_signals = set()


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


def post_to_discord(webhook_url, content):
    resp = requests.post(webhook_url, json={"content": content}, timeout=10)
    if resp.status_code not in (200, 204):
        print(f"Discord post failed: {resp.status_code} {resp.text[:200]}")


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
                    posted_signals.add(key)

            for sig in find_value_bets(event):
                key = f"val:{event_id}:{sig['outcome']}:{sig['book']}:{round(sig['edge'],2)}"
                if key not in posted_signals:
                    post_to_discord(VAL_WEBHOOK, format_value_bet(event, sig))
                    posted_signals.add(key)

        time.sleep(1)


def main():
    print(f"Signal bot starting at {datetime.now(timezone.utc).isoformat()}")
    while True:
        try:
            run_scan()
        except Exception as e:
            print(f"Scan error: {e}")
        if len(posted_signals) > 5000:
            posted_signals.clear()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
