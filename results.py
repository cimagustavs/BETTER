"""Results tracker: remembers each posted tip, settles it once the match finishes, and keeps a
running profit total (weekly + all-time). State is persisted to disk so it survives restarts —
point STATE_DIR at a Railway volume mount (e.g. /data) so it isn't wiped on redeploy.

Staking model: every tip is a flat 1% stake. A win adds (fair_odds - 1)%, a loss subtracts 1%.
"""
import os
import json
from datetime import datetime, timezone, timedelta

STATE_DIR = os.environ.get("STATE_DIR", "/data")
STATE_PATH = os.path.join(STATE_DIR, "results_state.json")


def _current_week_start():
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")


def _default_state():
    return {
        "pending": {},        # match_id -> bet dict awaiting settlement
        "settled": [],        # match_ids already settled (dedup)
        "all_time": 0.0,      # cumulative profit %
        "all_wins": 0,
        "all_losses": 0,
        "week_start": _current_week_start(),
        "weekly": 0.0,
        "week_wins": 0,
        "week_losses": 0,
        "win_rate_msg_id": None,  # id of the current #win-rate-tracker board, so it can be replaced
    }


def load_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        for k, v in _default_state().items():
            state.setdefault(k, v)
        return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()


def reset_state():
    """Wipe all tracked results back to zero and persist. Used to clear bad/test data."""
    state = _default_state()
    save_state(state)
    return state


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        print(f"[results] state save failed (is STATE_DIR a writable volume?): {e}")


def record_pending(state, bet):
    """bet: {match_id, home, away, competition, tip_label, tip_odds, tip_descriptor, kickoff}"""
    mid = str(bet["match_id"])
    if mid in state["pending"] or mid in state["settled"]:
        return False
    state["pending"][mid] = bet
    return True


def _roll_week(state):
    cur = _current_week_start()
    if state.get("week_start") != cur:
        state["week_start"] = cur
        state["weekly"] = 0.0
        state["week_wins"] = 0
        state["week_losses"] = 0


def apply_settlement(state, hit, odds):
    """Update running totals for one settled tip. Returns the profit delta (%) for this bet."""
    _roll_week(state)
    if hit:
        delta = (odds - 1.0)
        state["week_wins"] += 1
        state["all_wins"] += 1
    else:
        delta = -1.0
        state["week_losses"] += 1
        state["all_losses"] += 1
    state["weekly"] += delta
    state["all_time"] += delta
    return delta


def _sign(x):
    return f"+{x:.2f}" if x >= 0 else f"{x:.2f}"


def result_embed(bet, home_goals, away_goals, hit, delta, state, banner_filename=None):
    """Build the Discord embed for a settled tip card."""
    weekly = state["weekly"]
    alltime = state["all_time"]
    embed = {
        "title": "✅  STATISTICAL TIP HIT" if hit else "❌  STATISTICAL TIP MISSED",
        "color": 0x2ECC71 if hit else 0xE74C3C,
        "fields": [
            {"name": "🏟️ Match",
             "value": f"**{bet['home']} {home_goals}–{away_goals} {bet['away']}**\n_{bet['competition']}_",
             "inline": False},
            {"name": "🎯 Tip", "value": bet["tip_label"], "inline": True},
            {"name": "💰 Odds", "value": f"{bet['tip_odds']:.2f}", "inline": True},
            {"name": "📍 Result", "value": "WON ✅" if hit else "LOST ❌", "inline": True},
            {"name": "± This bet", "value": f"**{_sign(delta)}%**", "inline": True},
            {"name": "📅 This week",
             "value": f"**{_sign(weekly)}%**  ({state['week_wins']}W–{state['week_losses']}L)",
             "inline": True},
            {"name": "📈 All-time",
             "value": f"**{_sign(alltime)}%**  ({state['all_wins']}W–{state['all_losses']}L)",
             "inline": True},
        ],
        "footer": {"text": "Flat 1% stakes at the model's fair odds. Past results don't guarantee future outcomes — bet responsibly."},
    }
    if banner_filename:
        embed["image"] = {"url": f"attachment://{banner_filename}"}
    return embed
