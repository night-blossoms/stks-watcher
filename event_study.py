"""
Answers: "if the rule had fired on day t, what actually happened to the stock
over the next 1..10 trading days?" — an event study, not a single lagged
correlation number.

For every historical day where a commodity's 1-day move crossed the
threshold in rules.json, this walks the stock's forward cumulative return
(sign-adjusted for the rule's BUY/SELL call) day by day out to 10 sessions,
and averages across all such events. Where that average curve peaks tells
you how long the effect is actually worth holding for; where it turns
negative tells you when you'd be giving profit back.

Also reports win rate per horizon and the same split-half check as
analyze_correlation.py, so a peak that only shows up in one half of history
doesn't get mistaken for a real holding period.
"""
import json
import os

import numpy as np
import pandas as pd

import data as data_mod

MAX_HORIZON = 10
RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")


def load_rules():
    with open(RULES_PATH) as f:
        rules = json.load(f)
    rules.pop("_comment", None)
    return rules


def event_days(commodity_returns, threshold_pct):
    """Trading days where |1-day commodity return| >= threshold_pct."""
    return commodity_returns[commodity_returns.abs() >= threshold_pct]


def forward_returns(stock_close, event_dates, direction, commodity_returns, max_horizon=MAX_HORIZON):
    """For each event date t, sign-adjusted cumulative % return of stock from
    close[t] to close[t+h] for h=1..max_horizon. Sign is flipped so a
    correctly-called trade always shows as positive."""
    closes = stock_close
    idx = closes.index
    rows = []
    for t in event_dates:
        if t not in idx:
            continue
        pos = idx.get_loc(t)
        if pos + max_horizon >= len(idx):
            continue
        entry = closes.iloc[pos]
        moved_up = commodity_returns.loc[t] > 0
        called_buy = (direction == "positive" and moved_up) or (direction == "negative" and not moved_up)
        sign = 1 if called_buy else -1
        path = [(closes.iloc[pos + h] - entry) / entry * 100.0 * sign for h in range(1, max_horizon + 1)]
        rows.append(path)
    return pd.DataFrame(rows, columns=[f"h{h}" for h in range(1, max_horizon + 1)])


def analyze_pair(commodity, stock, cfg, history):
    symbol = data_mod.COMMODITIES[commodity]
    if symbol not in history or stock not in history:
        return None
    commodity_returns = history[symbol]["Close"].pct_change().dropna() * 100.0
    stock_close = history[stock]["Close"]

    events = event_days(commodity_returns, cfg["threshold_pct"])
    paths = forward_returns(stock_close, events.index, cfg["direction"], commodity_returns)
    if paths.empty or len(paths) < 8:
        return {"commodity": commodity, "stock": stock, "n_events": len(paths), "too_few": True}

    mid = len(paths) // 2
    avg = paths.mean()
    win_rate = (paths > 0).mean()
    avg_half1 = paths.iloc[:mid].mean()
    avg_half2 = paths.iloc[mid:].mean()

    best_h = int(avg.idxmax()[1:])  # column like "h3" -> 3
    return {
        "commodity": commodity, "stock": stock, "n_events": len(paths), "too_few": False,
        "avg_by_horizon": avg, "win_rate_by_horizon": win_rate,
        "avg_half1": avg_half1, "avg_half2": avg_half2,
        "suggested_hold_days": best_h, "peak_avg_return_pct": round(avg.max(), 3),
    }


def main():
    rules = load_rules()
    history = data_mod.fetch_all()

    print(f"{'commodity':8} {'stock':16} {'n_ev':5} {'best_h':6} {'peak_avg%':10} "
          f"{'win@best_h':10} {'h1_avg%':8} {'stable?':7}")

    results = []
    for commodity, stock_cfgs in rules.items():
        for stock, cfg in stock_cfgs.items():
            r = analyze_pair(commodity, stock, cfg, history)
            if r is None:
                continue
            if r["too_few"]:
                print(f"{commodity:8} {stock:16} {r['n_events']:5} -- too few events (>=8 needed) to trust")
                continue
            results.append(r)
            best_h = r["suggested_hold_days"]
            h_col = f"h{best_h}"
            stable = (np.sign(r["avg_half1"][h_col]) == np.sign(r["avg_half2"][h_col])
                      and min(abs(r["avg_half1"][h_col]), abs(r["avg_half2"][h_col])) > 0.05)
            print(f"{commodity:8} {stock:16} {r['n_events']:5} {best_h:6} "
                  f"{r['peak_avg_return_pct']:10} {r['win_rate_by_horizon'][h_col]:10.2f} "
                  f"{r['avg_by_horizon']['h1']:8.3f} {'yes' if stable else 'NO'}")

    print("\nFull day-by-day decay curves (avg sign-adjusted cumulative return %, by horizon):")
    for r in results:
        print(f"\n{r['commodity']} -> {r['stock']} (n={r['n_events']} events)")
        table = pd.DataFrame({
            "avg_return_pct": r["avg_by_horizon"].round(3),
            "win_rate": r["win_rate_by_horizon"].round(2),
            "half1_avg": r["avg_half1"].round(3),
            "half2_avg": r["avg_half2"].round(3),
        })
        print(table.to_string())

    print("\nRead this as: the horizon where avg_return_pct peaks (and half1/half2 agree in sign) "
          "is your evidence-based hold_days. If it keeps climbing past h10, the effect may last "
          "longer than 10 days — rerun with a larger MAX_HORIZON. If it peaks at h1 and then fades "
          "or reverses, exit fast; holding longer just gives the gain back.")


if __name__ == "__main__":
    main()
