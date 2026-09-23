# Commodity → Stock lag tracker (experimental, not financial advice)

Tests one hypothesis: gold/silver/copper moves lead certain Indian stocks by
1 day to 1 week, and that lag is exploitable. It isn't proven — this is the
kit to find out.

## Setup
```
pip install -r requirements.txt
```

## Step 1 — find real lags (do this first, don't skip)
```
python analyze_correlation.py
```
Pulls 2y of daily data for GC=F/SI=F/HG=F (gold/silver/copper futures) and
the mapped NSE stocks (see `data.py:STOCK_MAP`), then for lag = 0..7 trading
days prints lagged correlations, split-half stability, and a candidates
table (`lag_correlation_results.csv` has everything). Also checks
commodity-vs-commodity lags (does gold lead silver/copper).

Read the caveat it prints: a correlation is only worth acting on if it
survives the half-split check (same sign, meaningful size in both halves).
Most same-day (lag=0) links will be strong and unexploitable — the market
already priced them. You're hunting for lag ≥ 1 survivors.

## Step 2 — calibrate rules.json
Edit `rules.json` using what step 1 found: set `lag_days`, `window_days`,
`threshold_pct` per pair to match a real, stable correlation. **Already done
once** (2026-09-24) — see the `_comment`/`note` fields in `rules.json` and
`lag_correlation_results.csv`. Re-run periodically; a correlation found once
isn't guaranteed to hold forever.

## Step 2b — how long to hold (event_study.py)
A lag correlation tells you direction, not duration. `event_study.py`
answers "how long should I hold": for every historical day a commodity move
crossed its threshold, it traces the stock's average forward return day by
day out to 10 sessions, split into the first vs second half of history so a
decaying/reversing effect doesn't get mistaken for a stable one.
```
python event_study.py
```
Already run once — findings baked into `rules.json`'s `hold_days`:
- **silver → VEDL.NS** is the most durable: the average gain keeps growing
  through day 7, in both halves of history. Worth the longer hold (7 days).
- **gold → MUTHOOTFIN.NS** and **copper → HINDCOPPER.NS** peak early then
  *reverse* in the more recent half — hold_days set short (3 days) on
  purpose; going longer gave the gain back in this data.
- The other three (silver→Hindzinc, copper→Hindalco, copper→Vedanta) peak
  fast (2-3 days) and hold up in both halves.

Rerun this after a few months of new data — if the second-half decay you see
today keeps deepening, the edge may be getting arbitraged away.

## Step 3 — position sizing
`rules.json`'s `_position_sizing` block controls how much a paper trade bets:
```
position_value = paper_capital_inr * base_bet_pct_of_capital/100 * min(move_strength, max_size_multiplier)
```
where `move_strength = |commodity move| / threshold_pct` — a move that's 2x
the trigger threshold gets ~2x the normal bet, capped at
`max_size_multiplier`. `max_concurrent_positions` stops new trades from
opening once too many are already live. Defaults: ₹100,000 paper capital,
5% base bet, cap at 2.5x, max 6 concurrent positions — tune these to
whatever paper-capital size you actually want to simulate.

## Step 4 — run the daily trigger + paper trader
```
python tracker.py
```
Run this once per day after market close (cron/systemd once you host it).
Each run:
- evaluates every rule, logs **every** evaluation (triggered or not) to
  `signals_log.csv` — full audit trail, nothing throttled
- opens a dummy paper trade on new triggers (`paper_trades.csv`)
- closes paper trades once `hold_days` has elapsed and records P&L

No real orders are placed. Let it run for at least a few weeks of triggers
before trusting `paper_trades.csv`'s P&L — a handful of trades tells you
almost nothing.

Each trade also gets its own JSON file under `calls/call_00NN.json` — one
consistent template, written when the trade opens and rewritten in place
when it closes (entry/exit price, qty, predicted vs actual hold, P&L).

## Running it automatically (GitHub Actions + Pages)

`.github/workflows/daily-tracker.yml` runs `tracker.py` once every weekday
at 08:45 IST (before NSE opens at 09:15), commits the updated
`signals_log.csv` / `paper_trades.csv` / `calls/` / `docs/data.json` back to
the repo, and that's it — no server to maintain.

One-time setup after pushing this repo to GitHub:
1. Settings → Actions → General → under "Workflow permissions" pick
   **Read and write permissions** (the job needs to `git push` its results).
2. Settings → Pages → Source: **Deploy from a branch** → Branch **main**,
   folder **/docs** → Save.
3. Optionally Actions → "Daily commodity-stock tracker" → Run workflow, to
   trigger it once manually instead of waiting for the schedule.

The published site (`docs/`) is a small 4-page static app, no build step:
- **index.html** — every tracked commodity→stock pair, with a progress bar
  showing how close today's commodity move is to that rule's trigger
  threshold, plus a "watching / triggered today / position open" badge.
  Click a pair for its full rule config, today's reading, its open position
  (if any), and its past trades — plus one-click Google search links to that
  stock's price chart and the commodity's current price.
- **active.html** — every currently OPEN paper trade: entry price/qty, days
  held so far vs. planned hold, predicted exit date.
- **history.html** — every CLOSED trade, a per-pair accuracy/P&L breakdown,
  and the overall accuracy % / total P&L summary.

All four pages read the one `docs/data.json` the daily job regenerates —
there's no backend, it's just static files GitHub Pages serves.

## Known limitations
- yfinance data is free/delayed EOD-ish data, not real-time — fine for a
  daily rule, useless for intraday triggers.
- Daily-return correlations on a few hundred rows are noisy. A "stable"
  0.15 correlation will still be wrong often.
- The stock↔commodity mapping in `data.py` is a starting hypothesis, not
  exhaustive — add/remove tickers as you learn more.
- yfinance occasionally returns a trading day with NaN OHLC (seen live on
  NSE data) — `data.py` drops those rows automatically and prints a
  `[warn]` when it happens, so it never silently corrupts a price or a P&L.
