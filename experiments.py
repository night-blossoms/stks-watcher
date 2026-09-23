"""
Phase 1 experiment suite — pure analysis, does NOT touch rules.json or
tracker.py. Produces comparison tables so different candidate metrics can be
judged side by side before any of them get promoted into a live (paper)
rule. Safe to run any time; never invoked by the daily GitHub Actions job.

Experiments, all computed per (commodity, stock, lag=0..7):
  1. Pearson correlation (same method as analyze_correlation.py)
  2. Spearman rank correlation (robust to outlier/magnitude domination)
  3. Distance correlation (dcor) - catches non-linear dependence Pearson
     can miss entirely
  4. Market-neutral Pearson - stock return residualized against Nifty 50
     and, separately, Nifty 500, before correlating (strips general market
     beta so what's left is closer to a commodity-specific effect)
  5. USD/INR decomposition - for gold/silver/copper (already USD-quoted
     futures), correlates the USDINR return itself against each stock, so
     it's visible whether a "commodity" correlation is partly a rupee story
  6. Granger causality (statsmodels ssr F-test p-value, lag 1..7) - a
     formal "does the commodity's past actually help predict the stock"
     test, instead of the ad-hoc correlation+half-split heuristic
  7. Commodity-volume gating - for the 6 calibrated rules, splits historical
     trigger days into high vs low commodity-futures-volume and compares
     average forward return / win rate at that rule's own hold_days

Also reports a lower-bar (|corr| > 0.05) candidate list across an EXPANDED
stock map (adds back SBIN/HDFCBANK/ICICIBANK for gold+silver) purely for
exploration, clearly tagged separately from the validated 0.10 bar.
"""
import json
import os

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
import dcor
from statsmodels.tsa.stattools import grangercausalitytests

import data as data_mod
import market_factors as mf

MAX_LAG = 7
RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")

# Expanded beyond the live STOCK_MAP purely for the lower-bar exploratory
# scan (#1 in the plan) - banks vs gold/silver, previously rejected at the
# 0.10 bar, revisited here at 0.05 in case something weaker but real exists.
EXPANDED_STOCK_MAP = {
    commodity: dict(stocks) for commodity, stocks in data_mod.STOCK_MAP.items()
}
EXPANDED_STOCK_MAP["gold"].update({
    "SBIN.NS": "positive", "HDFCBANK.NS": "positive", "ICICIBANK.NS": "positive",
})
EXPANDED_STOCK_MAP["silver"] = dict(EXPANDED_STOCK_MAP.get("silver", {}))
EXPANDED_STOCK_MAP["silver"].update({
    "SBIN.NS": "positive", "HDFCBANK.NS": "positive", "ICICIBANK.NS": "positive",
})


def daily_returns(df):
    return df["Close"].pct_change().dropna() * 100.0


def half_split(a_aligned, b_aligned):
    mid = len(a_aligned) // 2
    c1 = a_aligned.iloc[:mid].corr(b_aligned.iloc[:mid])
    c2 = a_aligned.iloc[mid:].corr(b_aligned.iloc[mid:])
    return c1, c2


def lag_align(series_a, series_b, lag, min_overlap=60):
    """Return (a_aligned, b_aligned) for corr(a[t], b[t+lag]), or (None, None)."""
    b_shifted = series_b.shift(-lag)
    joined = pd.concat([series_a, b_shifted], axis=1, join="inner").dropna()
    if len(joined) < min_overlap:
        return None, None
    return joined.iloc[:, 0], joined.iloc[:, 1]


def stable(c1, c2, floor=0.05):
    return c1 is not None and c2 is not None and np.sign(c1) == np.sign(c2) and min(abs(c1), abs(c2)) > floor


def main():
    print("Fetching commodities, stocks, market indexes, USD/INR...")
    history = data_mod.fetch_all()
    for commodity_map in EXPANDED_STOCK_MAP.values():
        for stock in commodity_map:
            if stock not in history:
                try:
                    history[stock] = data_mod.fetch_history(stock)
                except Exception as exc:
                    print(f"[warn] could not fetch {stock}: {exc}")

    returns = {ticker: daily_returns(df) for ticker, df in history.items()}
    market_returns = mf.fetch_market_returns()   # nifty50, sensex, nifty500
    usdinr_returns = mf.fetch_usdinr_returns()

    # Precompute residualized stock returns per (stock, index) - independent
    # of which commodity we're scanning, so compute once per stock.
    residuals = {}  # (stock, index_name) -> residual Series
    all_stocks = {s for m in EXPANDED_STOCK_MAP.values() for s in m}
    for stock in all_stocks:
        if stock not in returns:
            continue
        for idx_name, idx_returns in market_returns.items():
            if idx_name == "sensex":
                continue  # sanity-check only, not part of the main comparison table
            res = mf.residualize(returns[stock], idx_returns)
            if res is not None:
                residuals[(stock, idx_name)] = res[0]  # (residual, alpha, beta)

    rows = []
    for commodity, symbol in data_mod.COMMODITIES.items():
        if symbol not in returns:
            continue
        commodity_ret = returns[symbol]
        for stock, expected_dir in EXPANDED_STOCK_MAP.get(commodity, {}).items():
            if stock not in returns:
                continue
            stock_ret = returns[stock]

            for lag in range(0, MAX_LAG + 1):
                a, b = lag_align(commodity_ret, stock_ret, lag)
                if a is None:
                    continue
                pearson = a.corr(b)
                spearman = a.corr(b, method="spearman")
                try:
                    dcor_val = dcor.distance_correlation(a.values, b.values)
                except Exception:
                    dcor_val = None
                c1, c2 = half_split(a, b)

                row = {
                    "commodity": commodity, "stock": stock, "lag_days": lag,
                    "n_obs": len(a), "expected_direction": expected_dir,
                    "pearson": round(pearson, 3),
                    "pearson_stable": stable(c1, c2),
                    "spearman": round(spearman, 3),
                    "dcor": round(dcor_val, 3) if dcor_val is not None else None,
                }

                # market-neutral: residualized stock vs same-lag commodity return
                for idx_name in ("nifty50", "nifty500"):
                    res_series = residuals.get((stock, idx_name))
                    col = f"pearson_{idx_name}_neutral"
                    if res_series is None:
                        row[col] = None
                        continue
                    ra, rb = lag_align(commodity_ret, res_series, lag)
                    row[col] = round(ra.corr(rb), 3) if ra is not None else None

                # USD/INR component: does the rupee move itself correlate
                # with the stock at this lag, independent of the commodity?
                fa, fb = lag_align(usdinr_returns, stock_ret, lag)
                row["pearson_usdinr_component"] = round(fa.corr(fb), 3) if fa is not None else None

                rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv("experiment_results.csv", index=False)
    print(f"\nWrote experiment_results.csv ({len(df)} rows)")

    print("\n=== Exploratory candidates: |pearson| > 0.05, lag >= 1, stable sign ===")
    exploratory = df[(df["lag_days"] >= 1) & (df["pearson"].abs() > 0.05) & (df["pearson_stable"])]
    exploratory = exploratory.sort_values("pearson", key=abs, ascending=False)
    print(exploratory[["commodity", "stock", "lag_days", "pearson", "spearman", "dcor",
                        "n_obs", "expected_direction"]].to_string(index=False))

    print("\n=== Market-neutral check on the 6 live rules (lag=1) ===")
    live_rules = _load_live_pairs()
    for commodity, stock in live_rules:
        r = df[(df["commodity"] == commodity) & (df["stock"] == stock) & (df["lag_days"] == 1)]
        if r.empty:
            continue
        r = r.iloc[0]
        print(f"{commodity:8} -> {stock:14} raw={r['pearson']:+.3f}  "
              f"nifty50-neutral={r['pearson_nifty50_neutral']}  "
              f"nifty500-neutral={r['pearson_nifty500_neutral']}  "
              f"usdinr-component={r['pearson_usdinr_component']}  "
              f"dcor={r['dcor']}  spearman={r['spearman']:+.3f}")

    print("\nInterpretation: if nifty-neutral is much smaller than raw, the raw")
    print("correlation is mostly market beta, not a commodity-specific effect.")
    print("If usdinr-component is comparable in size/sign to raw, part of the")
    print("'commodity' effect may really be a rupee-depreciation effect.")
    print("If dcor is notably larger than |pearson|, a non-linear relationship")
    print("Pearson is blind to may be present.")

    run_granger(history, returns, live_rules)
    run_volume_gating(history, returns, live_rules)


def _load_live_pairs():
    with open(RULES_PATH) as f:
        rules = json.load(f)
    rules.pop("_comment", None)
    rules.pop("_position_sizing", None)
    return [(c, s) for c, stocks in rules.items() for s in stocks]


def run_granger(history, returns, live_pairs):
    print("\n=== Granger causality (does commodity's past help predict stock's return?) ===")
    print(f"{'pair':30} " + " ".join(f"lag{i}" for i in range(1, MAX_LAG + 1)))
    granger_rows = []
    for commodity, stock in live_pairs:
        symbol = data_mod.COMMODITIES[commodity]
        if symbol not in returns or stock not in returns:
            continue
        joined = pd.concat([returns[stock], returns[symbol]], axis=1, join="inner").dropna()
        joined.columns = ["stock", "commodity"]
        if len(joined) < 100:
            continue
        try:
            result = grangercausalitytests(joined[["stock", "commodity"]], maxlag=MAX_LAG)
        except Exception as exc:
            print(f"[warn] granger test failed for {commodity}->{stock}: {exc}")
            continue
        pvals = [round(result[lag][0]["ssr_ftest"][1], 4) for lag in range(1, MAX_LAG + 1)]
        granger_rows.append({"commodity": commodity, "stock": stock,
                              **{f"pvalue_lag{lag}": p for lag, p in zip(range(1, MAX_LAG + 1), pvals)}})
        flags = " ".join(f"{p:.3f}{'*' if p < 0.05 else ' '}" for p in pvals)
        print(f"{commodity + '->' + stock:30} {flags}")

    pd.DataFrame(granger_rows).to_csv("granger_results.csv", index=False)
    print("(* = p<0.05, i.e. commodity's past significantly improves prediction at that lag)")
    print("Wrote granger_results.csv")


def run_volume_gating(history, returns, live_pairs):
    print("\n=== Commodity-volume gating: does a high-volume trigger day predict better? ===")
    with open(RULES_PATH) as f:
        rules = json.load(f)
    rules.pop("_comment", None)
    rules.pop("_position_sizing", None)

    vg_rows = []
    for commodity, stock in live_pairs:
        cfg = rules[commodity][stock]
        symbol = data_mod.COMMODITIES[commodity]
        if symbol not in history or stock not in history:
            continue
        commodity_df = history[symbol]
        commodity_ret = commodity_df["Close"].pct_change() * 100.0
        commodity_vol = commodity_df["Volume"]
        vol_median = commodity_vol.median()
        stock_close = history[stock]["Close"]
        idx = stock_close.index
        threshold = cfg["threshold_pct"]
        hold = cfg["hold_days"]
        direction = cfg["direction"]

        events = commodity_ret[commodity_ret.abs() >= threshold].dropna()
        high_returns, low_returns = [], []
        for t in events.index:
            if t not in idx or t not in commodity_vol.index:
                continue
            pos = idx.get_loc(t)
            if pos + hold >= len(idx):
                continue
            entry = stock_close.iloc[pos]
            exit_price = stock_close.iloc[pos + hold]
            moved_up = events.loc[t] > 0
            called_buy = (direction == "positive") == moved_up
            sign = 1 if called_buy else -1
            ret = (exit_price - entry) / entry * 100.0 * sign
            (high_returns if commodity_vol.loc[t] > vol_median else low_returns).append(ret)

        def summarize(vals):
            if not vals:
                return None, None
            arr = np.array(vals)
            return round(arr.mean(), 3), round((arr > 0).mean(), 3)

        hi_avg, hi_win = summarize(high_returns)
        lo_avg, lo_win = summarize(low_returns)
        vg_rows.append({
            "commodity": commodity, "stock": stock,
            "n_high_vol_events": len(high_returns), "high_vol_avg_return": hi_avg, "high_vol_win_rate": hi_win,
            "n_low_vol_events": len(low_returns), "low_vol_avg_return": lo_avg, "low_vol_win_rate": lo_win,
        })
        print(f"{commodity}->{stock}: high-vol n={len(high_returns)} avg={hi_avg} win={hi_win}  |  "
              f"low-vol n={len(low_returns)} avg={lo_avg} win={lo_win}")

    pd.DataFrame(vg_rows).to_csv("volume_gated_results.csv", index=False)
    print("Wrote volume_gated_results.csv")


if __name__ == "__main__":
    main()
