"""Point-in-time backtest of the ensemble model. For each match, the model is rebuilt using only
matches that finished BEFORE it (no lookahead), the match is predicted, then scored against the
actual result. Reports accuracy, calibration, Brier score, and how the 'strongest tip' would have
done at the model's own fair odds.

Usage: FD_API_KEY=... python3 backtest.py [COMPETITION_CODE] [SEASON]
"""
import os
import sys
import soccer_model as sm

sm.FD_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY") or os.environ["FD_API_KEY"]


def outcome(hg, ag):
    return "H" if hg > ag else ("A" if ag > hg else "D")


def run(code="PL", season="2025", warmup=60, national=False):
    data = sm.fd_get(f"/competitions/{code}/matches", {"status": "FINISHED", "season": season})
    matches = [m for m in (data or {}).get("matches", [])
               if m.get("score", {}).get("fullTime", {}).get("home") is not None]
    matches.sort(key=lambda m: m.get("utcDate", ""))
    print(f"{code} {season}: {len(matches)} finished matches\n")

    n = correct = 0
    brier_sum = 0.0
    tip_n = tip_hits = 0
    tip_profit = 0.0
    calib = {}  # bucket -> [hits, total] for the tip outcome

    for i in range(warmup, len(matches)):
        m = matches[i]
        stats = sm.build_team_stats(matches[:i], pooled=national)
        if not stats:
            continue
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        p = sm.match_probabilities(stats, home, away, min_games=(2 if national else 6))
        if not p:
            continue
        hg = m["score"]["fullTime"]["home"]
        ag = m["score"]["fullTime"]["away"]
        act = outcome(hg, ag)

        # result accuracy + Brier (multiclass)
        probs = {"H": p["home_win"], "D": p["draw"], "A": p["away_win"]}
        pred = max(probs, key=probs.get)
        n += 1
        if pred == act:
            correct += 1
        brier_sum += sum((probs[o] - (1.0 if o == act else 0.0)) ** 2 for o in "HDA")

        # strongest tip simulation (flat 1u at fair odds)
        label, tprob, todds, desc = sm.strongest_tip(p, home, away)
        hit = sm.settle_tip(desc, hg, ag)
        tip_n += 1
        tip_hits += 1 if hit else 0
        tip_profit += (todds - 1.0) if hit else -1.0
        b = round(tprob * 10) / 10  # 0.1-wide calibration bucket
        calib.setdefault(b, [0, 0])
        calib[b][0] += 1 if hit else 0
        calib[b][1] += 1

    if n == 0:
        print("Not enough data to backtest.")
        return

    print(f"Predictions scored: {n}")
    print(f"Result accuracy (top pick wins): {correct/n*100:.1f}%   (random ~ 40-45% in football)")
    print(f"Brier score (lower=better; 0.0=perfect, ~0.66=random 1/3): {brier_sum/n:.3f}")
    print()
    print(f"STRONGEST TIP (flat 1u at fair odds):")
    print(f"  tips: {tip_n}   hit rate: {tip_hits/tip_n*100:.1f}%   profit: {tip_profit:+.1f}u   ROI: {tip_profit/tip_n*100:+.1f}%")
    print()
    print("Calibration of the tip (does X% predicted win ~X%?):")
    print(f"  {'predicted':>10} | {'actual':>7} | {'n':>4}")
    for b in sorted(calib):
        hits, tot = calib[b]
        print(f"  {b*100:>8.0f}% | {hits/tot*100:>6.1f}% | {tot:>4}")
    print("\n_At fair odds a well-calibrated model nets ~0% ROI by construction — positive ROI here\n"
          " would mean the tips win MORE often than the model itself expects (underconfident)._")


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "PL"
    season = sys.argv[2] if len(sys.argv) > 2 else "2025"
    run(code, season, national=(code in sm.NATIONAL_TEAM_COMPETITIONS))
