"""
Finds delayed (lagged) correlations between:
  1. commodities and the Indian stocks mapped to them in data.STOCK_MAP
  2. the commodities themselves (does gold's move today predict silver's move
     tomorrow, etc.)

Lag convention: corr(commodity_return[t], target_return[t + lag]).
lag=0  -> same-day move (usually already priced in, not tradeable)
lag=1  -> commodity moves today, target moves tomorrow (tradeable if real)
lag=N  -> commodity move leads target by N trading days

This is NOT a rigorous backtest. Daily-return correlations on a few hundred
rows are noisy — a lag=3 correlation of 0.15 can easily be noise. To guard
against reading tea leaves, this script also does a crude stability check:
it splits the history in half and reports the same lag correlation on each
half separately. If a "signal" only shows up in one half, don't trust it.
"""
import itertools
import pandas as pd
import numpy as np

import data as data_mod

MAX_LAG_DAYS = 7  # user asked for 1 day to max 1 week


def daily_returns(df):
    return df["Close"].pct_change().dropna() * 100.0


def lagged_corr(series_a, series_b, lag, min_overlap=60):
    """corr(a[t], b[t+lag]) using their shared dates, shifting b back by lag."""
    a = series_a.copy()
    b = series_b.shift(-lag)  # b[t+lag] aligned to index t
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < min_overlap:
        return None, len(joined)
    corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    return corr, len(joined)


def half_split_stability(series_a, series_b, lag):
    """Same lagged correlation computed on the first half and second half of
    the overlapping history, so we can see if a correlation is consistent
    or just a fluke of one period."""
    a = series_a.copy()
    b = series_b.shift(-lag)
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < 120:
        return None, None
    mid = len(joined) // 2
    first = joined.iloc[:mid]
    second = joined.iloc[mid:]
    c1 = first.iloc[:, 0].corr(first.iloc[:, 1])
    c2 = second.iloc[:, 0].corr(second.iloc[:, 1])
    return c1, c2


def scan_pair(name_a, series_a, name_b, series_b, max_lag=MAX_LAG_DAYS):
    rows = []
    for lag in range(0, max_lag + 1):
        corr, n = lagged_corr(series_a, series_b, lag)
        if corr is None:
            continue
        c1, c2 = half_split_stability(series_a, series_b, lag)
        stable = (c1 is not None and c2 is not None and
                   np.sign(c1) == np.sign(c2) and min(abs(c1), abs(c2)) > 0.05)
        rows.append({
            "a": name_a, "b": name_b, "lag_days": lag,
            "corr": round(corr, 3), "n_obs": n,
            "half1_corr": round(c1, 3) if c1 is not None else None,
            "half2_corr": round(c2, 3) if c2 is not None else None,
            "stable_sign": stable,
        })
    return rows


def main():
    history = data_mod.fetch_all()
    returns = {ticker: daily_returns(df) for ticker, df in history.items()}

    all_rows = []

    # 1. commodity -> mapped stock
    for commodity, symbol in data_mod.COMMODITIES.items():
        if symbol not in returns:
            continue
        for stock, expected_dir in data_mod.STOCK_MAP[commodity].items():
            if stock not in returns:
                continue
            rows = scan_pair(commodity, returns[symbol], stock, returns[stock])
            for r in rows:
                r["expected_direction"] = expected_dir
            all_rows.extend(rows)

    # 2. commodity -> commodity (gold/silver/copper cross-lags)
    commodity_items = list(data_mod.COMMODITIES.items())
    for (name_a, sym_a), (name_b, sym_b) in itertools.permutations(commodity_items, 2):
        if sym_a not in returns or sym_b not in returns:
            continue
        rows = scan_pair(name_a, returns[sym_a], name_b, returns[sym_b])
        for r in rows:
            r["expected_direction"] = "n/a"
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("No data to analyze — check that fetching worked (see warnings above).")
        return

    df.to_csv("lag_correlation_results.csv", index=False)

    print("\n=== Top candidates: |corr| > 0.10 at lag 1-7 days, stable sign across both halves ===")
    candidates = df[(df["lag_days"] >= 1) & (df["corr"].abs() > 0.10) & (df["stable_sign"])]
    candidates = candidates.sort_values("corr", key=abs, ascending=False)
    if candidates.empty:
        print("None found. This is common — most obvious commodity/stock links get priced "
              "same-day (lag=0) and there's little real lagged edge left in daily closes. "
              "See the full table in lag_correlation_results.csv for lag=0 same-day strength.")
    else:
        print(candidates[["a", "b", "lag_days", "corr", "half1_corr", "half2_corr", "n_obs",
                            "expected_direction"]].to_string(index=False))

    print("\n=== Same-day (lag=0) reference, for comparison ===")
    same_day = df[df["lag_days"] == 0].sort_values("corr", key=abs, ascending=False)
    print(same_day[["a", "b", "corr", "n_obs", "expected_direction"]].to_string(index=False))

    print("\nFull results written to lag_correlation_results.csv")
    print("\nReminder: a correlation surviving the half-split stability check is a *hypothesis* "
          "worth paper-trading, not a proven edge. Small |corr| (0.1-0.2) means it will be wrong "
          "often even if real.")


if __name__ == "__main__":
    main()
