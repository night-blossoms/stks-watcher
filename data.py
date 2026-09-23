"""
Data access layer: pulls commodity + Indian stock price history via yfinance
and caches it to CSV so repeated runs don't re-hit the network every time.
"""
import os
import pandas as pd
import yfinance as yf

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")

COMMODITIES = {
    "gold": "GC=F",
    "silver": "SI=F",
    "copper": "HG=F",
}

# Which Indian-listed stocks each commodity is expected to move, and in which
# direction ("positive" = stock tends to rise when the commodity rises,
# "negative" = stock tends to fall when the commodity rises).
STOCK_MAP = {
    "gold": {
        "MUTHOOTFIN.NS": "positive",   # gold-loan NBFC, collateral value up
        "MANAPPURAM.NS": "positive",   # gold-loan NBFC
        "TITAN.NS": "negative",        # jewellery, expensive gold dents volume
    },
    "silver": {
        "HINDZINC.NS": "positive",     # silver is a byproduct of zinc mining
        "VEDL.NS": "positive",
    },
    "copper": {
        "HINDCOPPER.NS": "positive",   # pure-play copper miner
        "HINDALCO.NS": "positive",
        "VEDL.NS": "positive",
    },
    # Tested and rejected: SBIN.NS, HDFCBANK.NS, ICICIBANK.NS vs gold/silver.
    # Banks have no direct commodity exposure — any link would have to run
    # through the slower macro chain (inflation -> RBI policy -> bond yields
    # -> bank margins). No lag-1 correlation exceeded 0.10 for any bank; see
    # REFERENCES.md for the full numbers.
}


def all_tickers():
    tickers = set(COMMODITIES.values())
    for stock_map in STOCK_MAP.values():
        tickers.update(stock_map.keys())
    return sorted(tickers)


def _cache_path(ticker):
    safe = ticker.replace("=", "_").replace(".", "_")
    return os.path.join(CACHE_DIR, f"{safe}.csv")


def fetch_history(ticker, period="2y", force_refresh=False):
    """Fetch daily OHLCV history for one ticker, using a local CSV cache."""
    path = _cache_path(ticker)
    if not force_refresh and os.path.exists(path):
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if not df.empty:
            return df.dropna(subset=["Close"])

    df = yf.Ticker(ticker).history(period=period, interval="1d")
    if df.empty:
        raise RuntimeError(f"No data returned for {ticker} — check the symbol or your network.")
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    # yfinance occasionally returns a trading day with NaN OHLC (seen live on
    # VEDL.NS: a bar with nonzero volume but every price field NaN — an
    # upstream data glitch, not a holiday/no-trade day). Treating a NaN close
    # as "the latest price" would silently corrupt every downstream
    # calculation: exit fills, P&L, threshold comparisons (NaN >= x is always
    # False, so a real signal could go undetected with no error raised).
    bad_rows = df["Close"].isna().sum()
    if bad_rows:
        print(f"[warn] {ticker}: dropping {bad_rows} row(s) with NaN Close (bad upstream data)")
        df = df.dropna(subset=["Close"])
    # Different exchanges return tz-aware indices with different UTC offsets
    # (NSE +05:30 vs COMEX -04:00/-05:00). Both timestamps are local midnight
    # for their own exchange, so stripping tz and keeping the date lets
    # commodity and stock series align on trading date instead of exact
    # timestamp (which would otherwise never match across exchanges).
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(path)
    return df


def fetch_all(period="2y", force_refresh=False):
    """Fetch history for every ticker this project cares about."""
    out = {}
    for ticker in all_tickers():
        try:
            out[ticker] = fetch_history(ticker, period=period, force_refresh=force_refresh)
        except Exception as exc:
            print(f"[warn] could not fetch {ticker}: {exc}")
    return out


def latest_pct_change(df, window_days=1):
    """% change of Close from `window_days` trading days ago to the most recent close."""
    closes = df["Close"].dropna()
    if len(closes) < window_days + 1:
        return None
    recent = closes.iloc[-1]
    past = closes.iloc[-1 - window_days]
    return (recent - past) / past * 100.0


if __name__ == "__main__":
    data = fetch_all(force_refresh=True)
    for ticker, df in data.items():
        print(f"{ticker}: {len(df)} rows, last close {df['Close'].iloc[-1]:.2f} on {df.index[-1].date()}")
