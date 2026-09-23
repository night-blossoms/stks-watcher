"""
Market-neutral and currency-decomposition helpers for the experiment suite.

Not used by tracker.py / the live daily job — these tickers are deliberately
kept out of data.COMMODITIES / data.STOCK_MAP so they never get pulled into
the live fetch_all() or evaluated as tradeable rules. This module is only
imported by experiments.py.
"""
import numpy as np
import pandas as pd

import data as data_mod

MARKET_INDEXES = {
    "nifty50": "^NSEI",
    "sensex": "^BSESN",
    "nifty500": "^CRSLDX",
}
USDINR_TICKER = "INR=X"


def daily_returns(df):
    return df["Close"].pct_change().dropna() * 100.0


def fetch_market_returns():
    """Returns {'nifty50': Series, 'sensex': Series, 'nifty500': Series}."""
    out = {}
    for name, ticker in MARKET_INDEXES.items():
        try:
            out[name] = daily_returns(data_mod.fetch_history(ticker))
        except Exception as exc:
            print(f"[warn] could not fetch {name} ({ticker}): {exc}")
    return out


def fetch_usdinr_returns():
    return daily_returns(data_mod.fetch_history(USDINR_TICKER))


def residualize(stock_returns, market_returns, min_overlap=60):
    """OLS stock_return = alpha + beta*market_return; return the residual
    series (the stock's move *not* explained by the general market that
    day), aligned to stock_returns' index. None if not enough overlap."""
    joined = pd.concat([stock_returns, market_returns], axis=1, join="inner").dropna()
    joined.columns = ["stock", "market"]
    if len(joined) < min_overlap:
        return None
    beta, alpha = np.polyfit(joined["market"], joined["stock"], 1)
    predicted = alpha + beta * joined["market"]
    residual = joined["stock"] - predicted
    return residual, alpha, beta


def decompose_commodity_return(commodity_usd_returns, usdinr_returns, min_overlap=60):
    """A commodity's INR-terms daily return is approximately its USD-price
    return plus the USD/INR return (compounding cross-term ignored, it's
    negligible at daily-return magnitudes). Returns
    (usd_component, fx_component) both aligned to the shared dates, so each
    can be correlated against a stock separately to see which piece is
    actually doing the work."""
    joined = pd.concat([commodity_usd_returns, usdinr_returns], axis=1, join="inner").dropna()
    joined.columns = ["commodity_usd", "usdinr"]
    if len(joined) < min_overlap:
        return None, None
    return joined["commodity_usd"], joined["usdinr"]
