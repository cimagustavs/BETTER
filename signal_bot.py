import os
import json
import time
import requests
from datetime import datetime, timezone

import soccer_model
import results

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
ARB_WEBHOOK = os.environ["ARB_WEBHOOK"]
VAL_WEBHOOK = os.environ["VAL_WEBHOOK"]
FREE_WEBHOOK = os.environ["FREE_WEBHOOK"]
SILVER_WEBHOOK = os.environ["SILVER_WEBHOOK"]
GOLD_WEBHOOK = os.environ["GOLD_WEBHOOK"]
HIT_HISTORY_WEBHOOK = os.environ.get("HIT_HISTORY_WEBHOOK")  # optional; results tracker off if unset
DAILY_LOCK_WEBHOOK = os.environ.get("DAILY_LOCK_WEBHOOK")    # free daily teaser pick
PARLAY_WEBHOOK = os.environ.get("PARLAY_WEBHOOK")            # gold daily parlay
BANKROLL_WEBHOOK = os.environ.get("BANKROLL_WEBHOOK")        # gold low-variance picks

PREMIUM_THRESHOLD = 0.55  # top outcome >= this is flagged a Gold "premium play"

soccer_model.FD_API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]

# Optional banner image attached to each result card (path inside the repo). Degrades gracefully if missing.
BANNER_PATH = os.environ.get("BANNER_PATH", os.path.join(os.path.dirname(__file__), "assets", "tip_banner.png"))

# Per-competition channel webhooks (optional — only routed if the env var is set).
COMPETITION_WEBHOOKS = {
    code: os.environ[f"COMP_{code}_WEBHOOK"]
    for code in ("PL", "PD", "SA", "BL1", "FL1", "CL", "WC")
    if os.environ.get(f"COMP_{code}_WEBHOOK")
}

# Results tracker state. Set RESET_STATE=1 once (then remove it) to wipe a bad/test tally.
if os.environ.get("RESET_STATE"):
    RESULTS_STATE = results.reset_state()
    print("[results] RESET_STATE set — tally wiped to zero.")
else:
    RESULTS_STATE = results.load_state()

SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_fifa_world_cup",
]
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


def post_to_discord(webhook_url, content=None, embed=None):
    payload = {}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embeds"] = [embed]
    for attempt in range(3):
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.2)
            continue
        print(f"Discord post failed: {resp.status_code} {resp.text[:200]}")
        return
    print("Discord post gave up after rate limiting")


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


SOCCER_LEAD_TIME_SECONDS = 2 * 3600  # start posting a match once it's within ~2h of kickoff

posted_soccer_matches = set()


def run_soccer_scan():
    """Primary signal: Poisson-model win/draw/loss probabilities for upcoming soccer matches,
    built from each team's actual historical goals scored/conceded. Each match is posted once,
    individually, roughly 2 hours before its kickoff — not all at once. Routed by how lopsided
    the model's probability spread is (a statistical confidence proxy, not a prediction)."""
    from datetime import datetime as _dt

    now = datetime.now(timezone.utc)

    for code, name in soccer_model.COMPETITIONS.items():
        try:
            matches = soccer_model.scan_competition(code, name)
        except Exception as e:
            print(f"Soccer scan failed for {name}: {e}")
            continue

        for match in matches:
            match_id = match["match_id"]
            if match_id in posted_soccer_matches:
                continue
            try:
                kickoff_dt = _dt.fromisoformat(match["kickoff"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            seconds_to_kickoff = (kickoff_dt - now).total_seconds()
            # Post once a match comes within the lead time and hasn't kicked off yet.
            # Dedup (posted_soccer_matches) guarantees it's still posted only once, and this is
            # robust to scan-timing drift: a match is caught on the first scan after it crosses
            # the 2h mark instead of needing a scan to land inside a narrow window.
            if seconds_to_kickoff <= 0 or seconds_to_kickoff > SOCCER_LEAD_TIME_SECONDS:
                continue

            probs = match["probs"]
            top_prob = max(probs["home_win"], probs["draw"], probs["away_win"])
            premium = top_prob >= PREMIUM_THRESHOLD

            free_embed = soccer_model.embed_for(match, include_tip=False)
            paid_embed = soccer_model.embed_for(match, include_tip=True)
            gold_embed = soccer_model.embed_for(match, include_tip=True, premium=premium)

            # FREE: full analysis but the actual tip is hidden behind a VIP teaser.
            # Goes to the public competition channel and #free-picks.
            comp_webhook = COMPETITION_WEBHOOKS.get(code)
            if comp_webhook:
                post_to_discord(comp_webhook, embed=free_embed)
            post_to_discord(FREE_WEBHOOK, embed=free_embed)

            # PAID: Silver and Gold both get the tip on every match; Gold flags premium plays.
            post_to_discord(SILVER_WEBHOOK, embed=paid_embed)
            post_to_discord(GOLD_WEBHOOK, embed=gold_embed)
            posted_soccer_matches.add(match_id)

            # Record the tip for later settlement in #hit-history.
            if HIT_HISTORY_WEBHOOK:
                results.record_pending(RESULTS_STATE, {
                    "match_id": match_id,
                    "code": code,
                    "home": match["home"],
                    "away": match["away"],
                    "competition": match["competition"],
                    "tip_label": match["tip_label"],
                    "tip_odds": match["tip_odds"],
                    "tip_descriptor": match["tip_descriptor"],
                    "kickoff": match["kickoff"],
                })
                results.save_state(RESULTS_STATE)

        time.sleep(2)


def post_result_card(embed):
    """Post a settled-tip card to #hit-history, attaching the banner image if one is bundled."""
    banner = os.path.basename(BANNER_PATH)
    if os.path.isfile(BANNER_PATH):
        embed = dict(embed, image={"url": f"attachment://{banner}"})
        with open(BANNER_PATH, "rb") as f:
            files = {"file": (banner, f.read(), "image/png")}
        data = {"payload_json": json.dumps({"embeds": [embed]})}
        for _ in range(3):
            r = requests.post(HIT_HISTORY_WEBHOOK, data=data, files=files, timeout=15)
            if r.status_code in (200, 204):
                return
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1) + 0.2)
                continue
            print(f"hit-history post failed: {r.status_code} {r.text[:200]}")
            return
    else:
        post_to_discord(HIT_HISTORY_WEBHOOK, embed=embed)


def run_settlement_scan():
    """Settle any pending tips whose match has finished: evaluate hit/miss, update running
    profit, and post a result card to #hit-history."""
    if not HIT_HISTORY_WEBHOOK:
        return
    pending = dict(RESULTS_STATE.get("pending", {}))
    changed = False
    for mid, bet in pending.items():
        try:
            result = soccer_model.get_match_result(bet["code"], bet["match_id"])
        except Exception as e:
            print(f"Settlement fetch failed for {bet.get('home')} v {bet.get('away')}: {e}")
            continue
        if result is None:
            continue  # not finished yet
        hg, ag = result
        hit = soccer_model.settle_tip(tuple(bet["tip_descriptor"]), hg, ag)
        delta = results.apply_settlement(RESULTS_STATE, hit, bet["tip_odds"])
        embed = results.result_embed(bet, hg, ag, hit, delta, RESULTS_STATE)
        post_result_card(embed)

        RESULTS_STATE["pending"].pop(mid, None)
        RESULTS_STATE["settled"].append(mid)
        changed = True
    if changed:
        # keep the settled list from growing unbounded
        RESULTS_STATE["settled"] = RESULTS_STATE["settled"][-2000:]
        results.save_state(RESULTS_STATE)


def _gather_upcoming(hours_ahead=30):
    """All upcoming matches across competitions kicking off within the next `hours_ahead`,
    each with its tip, sorted by tip probability (strongest first)."""
    from datetime import datetime as _dt
    now = datetime.now(timezone.utc)
    out = []
    for code, name in soccer_model.COMPETITIONS.items():
        try:
            matches = soccer_model.scan_competition(code, name)
        except Exception as e:
            print(f"Daily gather failed for {name}: {e}")
            continue
        for match in matches:
            try:
                ko = _dt.fromisoformat(match["kickoff"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            secs = (ko - now).total_seconds()
            if 0 < secs <= hours_ahead * 3600:
                out.append(match)
        time.sleep(1)
    out.sort(key=lambda m: m["tip_prob"], reverse=True)
    return out


def run_daily_specials():
    """Once per UTC day: post the free daily lock, the Gold parlay, and Gold bankroll-builder picks."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if RESULTS_STATE.get("daily_specials_date") == today:
        return
    if not (DAILY_LOCK_WEBHOOK or PARLAY_WEBHOOK or BANKROLL_WEBHOOK):
        return

    upcoming = _gather_upcoming()
    if not upcoming:
        return

    # FREE: tease the day's strongest play but keep the actual pick locked behind VIP.
    if DAILY_LOCK_WEBHOOK:
        best = upcoming[0]
        embed = soccer_model.embed_for(best, include_tip=False)
        embed["title"] = "🔒 LOCK OF THE DAY  •  " + embed["title"]
        for f in embed["fields"]:
            if "Tip" in f["name"]:
                f["value"] = (
                    "🔒 This is the model's **single highest-confidence pick of the day** — "
                    "its strongest lean across every match.\nThe exact bet is **VIP only**. "
                    "Upgrade to **Silver** or **Gold** to unlock it (plus every other tip today)."
                )
        post_to_discord(DAILY_LOCK_WEBHOOK, embed=embed)

    # GOLD: parlay of the top legs (combined odds = product of fair odds).
    if PARLAY_WEBHOOK and len(upcoming) >= 2:
        legs = upcoming[:3]
        combined_odds = 1.0
        combined_prob = 1.0
        leg_lines = []
        for m in legs:
            combined_odds *= m["tip_odds"]
            combined_prob *= m["tip_prob"]
            leg_lines.append(f"• **{m['home']} vs {m['away']}** — {m['tip_label']} `@{m['tip_odds']:.2f}`")
        post_to_discord(PARLAY_WEBHOOK, embed={
            "title": "🎲 Parlay of the Day",
            "color": 0xF1C40F,
            "description": "\n".join(leg_lines) +
                           f"\n\n**Combined odds:** `{combined_odds:.2f}`\n"
                           f"**Model probability all hit:** `{combined_prob*100:.1f}%`",
            "footer": {"text": "Parlays are high-variance by design — stake small. Statistical estimate, not a guaranteed outcome."},
        })

    # GOLD: low-variance bankroll-builder — the day's safest higher-probability singles.
    if BANKROLL_WEBHOOK:
        safe = upcoming[:4]
        lines = [f"• **{m['home']} vs {m['away']}** — {m['tip_label']} `@{m['tip_odds']:.2f}`  ({m['tip_prob']*100:.0f}%)"
                 for m in safe]
        post_to_discord(BANKROLL_WEBHOOK, embed={
            "title": "🏦 Bankroll Builder — Today's Safest Singles",
            "color": 0x2ECC71,
            "description": "\n".join(lines) +
                           "\n\n_Higher-probability, lower-variance singles for steady growth. Flat stakes recommended._",
            "footer": {"text": "Statistical estimate, not a guaranteed outcome — bet responsibly."},
        })

    RESULTS_STATE["daily_specials_date"] = today
    results.save_state(RESULTS_STATE)


def main():
    print(f"Signal bot starting at {datetime.now(timezone.utc).isoformat()}")
    while True:
        try:
            run_scan()
            run_soccer_scan()
            run_settlement_scan()
            run_daily_specials()
        except Exception as e:
            print(f"Scan error: {e}")
        if len(posted_signals) > 5000:
            posted_signals.clear()
        if len(posted_soccer_matches) > 5000:
            posted_soccer_matches.clear()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
