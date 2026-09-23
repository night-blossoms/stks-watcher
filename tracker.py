"""
Daily rule-based trigger + dummy paper-trading ledger.

Run this once per day (after market close, via cron/systemd — see README).
Each run:
  1. Evaluates every rule in rules.json against the latest cached prices.
  2. Logs EVERY evaluation (triggered or not) to signals_log.csv — nothing is
     skipped or throttled, so you get a full audit trail of what the rules
     saw each day, not just the days something fired.
  3. For newly triggered rules (and only if there isn't already an OPEN
     paper trade for that exact commodity/stock pair), opens a dummy trade
     at the stock's latest close.
  4. Closes any OPEN paper trade whose hold_days has elapsed and records its
     hypothetical P&L.

This does not place real orders and does not touch real money. It exists to
let you test whether a rule's signals would have made money, before you ever
risk anything on it.
"""
import json
import math
import os
from datetime import datetime, date, timedelta

import pandas as pd

import data as data_mod

BASE_DIR = os.path.dirname(__file__)
RULES_PATH = os.path.join(BASE_DIR, "rules.json")
SIGNALS_LOG = os.path.join(BASE_DIR, "signals_log.csv")
TRADES_LOG = os.path.join(BASE_DIR, "paper_trades.csv")
CALLS_DIR = os.path.join(BASE_DIR, "calls")
DOCS_DATA_PATH = os.path.join(BASE_DIR, "docs", "data.json")

SIGNAL_COLUMNS = ["run_date", "commodity", "commodity_pct_change", "window_days",
                   "stock", "direction_rule", "threshold_pct", "triggered", "signal"]
TRADE_COLUMNS = ["id", "commodity", "stock", "signal", "entry_date", "entry_price",
                  "quantity", "position_value_inr", "hold_days", "exit_date", "exit_price",
                  "status", "pnl_pct", "pnl_inr"]

DEFAULT_POSITION_SIZING = {
    "paper_capital_inr": 100000, "max_concurrent_positions": 6,
    "base_bet_pct_of_capital": 5, "max_size_multiplier": 2.5,
}


def load_rules():
    with open(RULES_PATH) as f:
        rules = json.load(f)
    rules.pop("_comment", None)
    sizing = rules.pop("_position_sizing", DEFAULT_POSITION_SIZING)
    sizing.pop("_explanation", None)
    return rules, sizing


def load_csv(path, columns):
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame(columns=columns)


def _none_if_blank(value):
    if value is None or value == "" or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def build_call_dict(row, rule_cfg):
    entry_date = row["entry_date"]
    hold_days = int(row["hold_days"])
    predicted_exit = entry_date + timedelta(days=hold_days) if isinstance(entry_date, date) else None
    return {
        "id": int(row["id"]),
        "commodity": row["commodity"],
        "stock": row["stock"],
        "signal": row["signal"],
        "trigger_date": str(entry_date),
        "entry_price": float(row["entry_price"]),
        "quantity": float(row["quantity"]),
        "position_value_inr": float(row["position_value_inr"]),
        "hold_days": hold_days,
        "predicted_exit_date": str(predicted_exit) if predicted_exit else None,
        "status": row["status"],
        "exit_date": (str(row["exit_date"]) if _none_if_blank(row["exit_date"]) is not None else None),
        "exit_price": (float(row["exit_price"]) if _none_if_blank(row["exit_price"]) is not None else None),
        "pnl_pct": (float(row["pnl_pct"]) if _none_if_blank(row["pnl_pct"]) is not None else None),
        "pnl_inr": (float(row["pnl_inr"]) if _none_if_blank(row["pnl_inr"]) is not None else None),
        "rule": {
            "threshold_pct": rule_cfg.get("threshold_pct"),
            "window_days": rule_cfg.get("window_days"),
            "lag_days": rule_cfg.get("lag_days"),
            "observed_corr": rule_cfg.get("observed_corr"),
            "note": rule_cfg.get("note", ""),
        },
        "disclaimer": "Paper trade only, no real money. Entry/exit prices are the latest "
                       "available daily close from free EOD data (yfinance), not a live "
                       "intraday fill.",
    }


def write_call_file(row, rule_cfg):
    os.makedirs(CALLS_DIR, exist_ok=True)
    path = os.path.join(CALLS_DIR, f"call_{int(row['id']):04d}.json")
    with open(path, "w") as f:
        json.dump(build_call_dict(row, rule_cfg), f, indent=2)
    return path


def evaluate_rules(rules, history, run_date):
    signal_rows = []
    triggers = []  # (commodity, stock, signal, entry_price, hold_days, move_strength)

    for commodity, symbol in data_mod.COMMODITIES.items():
        if symbol not in history:
            continue
        commodity_rules = rules.get(commodity, {})
        for stock, cfg in commodity_rules.items():
            if stock not in history:
                continue
            window_days = cfg["window_days"]
            threshold = cfg["threshold_pct"]
            direction = cfg["direction"]

            pct_change = data_mod.latest_pct_change(history[symbol], window_days)
            if pct_change is None:
                continue

            triggered = abs(pct_change) >= threshold
            signal = None
            if triggered:
                moved_up = pct_change > 0
                if direction == "positive":
                    signal = "BUY" if moved_up else "SELL"
                else:  # negative relationship
                    signal = "SELL" if moved_up else "BUY"

            signal_rows.append({
                "run_date": run_date, "commodity": commodity,
                "commodity_pct_change": round(pct_change, 3), "window_days": window_days,
                "stock": stock, "direction_rule": direction, "threshold_pct": threshold,
                "triggered": triggered, "signal": signal or "",
            })

            if triggered:
                entry_price = history[stock]["Close"].iloc[-1]
                move_strength = abs(pct_change) / threshold
                triggers.append((commodity, stock, signal, entry_price, cfg["hold_days"], move_strength))

    return signal_rows, triggers


def update_paper_trades(trades_df, triggers, run_date, sizing, rules):
    trades_df = trades_df.copy()
    if not trades_df.empty:
        trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"]).dt.date
        # A CSV round-trip turns an all-blank exit_date/exit_price/pnl_pct/pnl_inr
        # column into float64 (all-NaN) when every OPEN row has "" in it. Force
        # these back to object dtype so a date or price can be written into them
        # later without pandas raising a dtype error on close.
        for col in ("exit_date", "exit_price", "pnl_pct", "pnl_inr"):
            trades_df[col] = trades_df[col].astype(object).where(trades_df[col].notna(), "")

    # close matured OPEN trades
    for idx, row in trades_df.iterrows():
        if row["status"] != "OPEN":
            continue
        held = (run_date - row["entry_date"]).days
        if held >= row["hold_days"]:
            stock = row["stock"]
            try:
                exit_price = data_mod.fetch_history(stock)["Close"].iloc[-1]
            except Exception:
                continue
            direction_mult = 1 if row["signal"] == "BUY" else -1
            pnl_pct = (exit_price - row["entry_price"]) / row["entry_price"] * 100.0 * direction_mult
            pnl_inr = pnl_pct / 100.0 * row["position_value_inr"]
            trades_df.at[idx, "exit_date"] = run_date
            trades_df.at[idx, "exit_price"] = round(exit_price, 2)
            trades_df.at[idx, "status"] = "CLOSED"
            trades_df.at[idx, "pnl_pct"] = round(pnl_pct, 2)
            trades_df.at[idx, "pnl_inr"] = round(pnl_inr, 2)
            rule_cfg = rules.get(row["commodity"], {}).get(row["stock"], {})
            write_call_file(trades_df.loc[idx], rule_cfg)

    # open new trades, skipping pairs that already have an OPEN position and
    # respecting the max_concurrent_positions cap
    open_pairs = set()
    open_count = 0
    if not trades_df.empty:
        open_mask = trades_df["status"] == "OPEN"
        open_pairs = set(zip(trades_df.loc[open_mask, "commodity"], trades_df.loc[open_mask, "stock"]))
        open_count = int(open_mask.sum())

    base_bet = sizing["paper_capital_inr"] * sizing["base_bet_pct_of_capital"] / 100.0
    max_positions = sizing["max_concurrent_positions"]

    next_id = (trades_df["id"].max() + 1) if not trades_df.empty else 1
    new_rows = []
    for commodity, stock, signal, entry_price, hold_days, move_strength in triggers:
        if (commodity, stock) in open_pairs:
            continue
        if open_count >= max_positions:
            print(f"  [skip] {commodity}->{stock} signal fired but max_concurrent_positions "
                  f"({max_positions}) reached — not opening a paper trade")
            continue
        size_multiplier = min(move_strength, sizing["max_size_multiplier"])
        position_value = base_bet * size_multiplier
        quantity = position_value / float(entry_price)
        new_row = {
            "id": next_id, "commodity": commodity, "stock": stock, "signal": signal,
            "entry_date": run_date, "entry_price": round(float(entry_price), 2),
            "quantity": round(quantity, 4), "position_value_inr": round(position_value, 2),
            "hold_days": hold_days, "exit_date": "", "exit_price": "",
            "status": "OPEN", "pnl_pct": "", "pnl_inr": "",
        }
        new_rows.append(new_row)
        rule_cfg = rules.get(commodity, {}).get(stock, {})
        write_call_file(pd.Series(new_row), rule_cfg)
        open_pairs.add((commodity, stock))
        open_count += 1
        next_id += 1

    if new_rows:
        trades_df = pd.concat([trades_df, pd.DataFrame(new_rows)], ignore_index=True)
    return trades_df


def pair_id(commodity, stock):
    return f"{commodity}__{stock}"


def write_docs_data(trades_df, sizing, run_date, rules, signal_rows):
    """Aggregate rules.json + today's evaluations + paper_trades.csv into
    docs/data.json for the GitHub Pages site."""
    os.makedirs(os.path.dirname(DOCS_DATA_PATH), exist_ok=True)

    if trades_df.empty:
        open_trades, closed_trades = [], []
    else:
        df = trades_df.copy()
        df["pair_id"] = df["commodity"] + "__" + df["stock"]
        open_trades = df[df["status"] == "OPEN"].sort_values("entry_date", ascending=False).to_dict("records")
        closed_trades = df[df["status"] == "CLOSED"].sort_values("exit_date", ascending=False).to_dict("records")

    def json_safe(v):
        if v is None:
            return None
        if hasattr(v, "item"):  # numpy scalar -> native python
            v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
        if isinstance(v, str) and v == "":
            return None
        if isinstance(v, (pd.Timestamp, date, datetime)):
            return str(v)
        return v

    def clean(records):
        return [{k: json_safe(v) for k, v in r.items()} for r in records]

    open_trades, closed_trades = clean(open_trades), clean(closed_trades)
    open_by_pair = {t["commodity"] + "__" + t["stock"]: t for t in open_trades}

    # today's evaluation per pair, keyed for the tracking-stage page
    latest_eval = {(r["commodity"], r["stock"]): r for r in signal_rows}

    rules_status = []
    for commodity, stock_cfgs in rules.items():
        for stock, cfg in stock_cfgs.items():
            pid = pair_id(commodity, stock)
            ev = latest_eval.get((commodity, stock))
            move = ev["commodity_pct_change"] if ev else None
            progress = round(abs(move) / cfg["threshold_pct"], 3) if move is not None else None
            rules_status.append({
                "pair_id": pid, "commodity": commodity, "stock": stock,
                "direction": cfg["direction"], "threshold_pct": cfg["threshold_pct"],
                "window_days": cfg["window_days"], "lag_days": cfg.get("lag_days"),
                "hold_days": cfg["hold_days"], "observed_corr": cfg.get("observed_corr"),
                "note": cfg.get("note", ""),
                "latest_run_date": str(ev["run_date"]) if ev else None,
                "latest_commodity_pct_change": move,
                "progress_to_threshold": progress,
                "triggered_today": bool(ev["triggered"]) if ev else False,
                "open_trade": open_by_pair.get(pid),
            })

    closed = trades_df[trades_df["status"] == "CLOSED"] if not trades_df.empty else pd.DataFrame()
    n_closed = len(closed)
    n_wins = int((closed["pnl_inr"] > 0).sum()) if n_closed else 0
    total_pnl_inr = float(closed["pnl_inr"].sum()) if n_closed else 0.0
    avg_pnl_pct = float(closed["pnl_pct"].mean()) if n_closed else 0.0
    deployed = float(trades_df.loc[trades_df["status"] == "OPEN", "position_value_inr"].sum()) if not trades_df.empty else 0.0

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_date": str(run_date),
        "paper_capital_inr": sizing["paper_capital_inr"],
        "summary": {
            "open_positions": int((trades_df["status"] == "OPEN").sum()) if not trades_df.empty else 0,
            "capital_deployed_inr": round(deployed, 2),
            "closed_trades": n_closed,
            "wins": n_wins,
            "accuracy_pct": round(n_wins / n_closed * 100, 1) if n_closed else None,
            "total_pnl_inr": round(total_pnl_inr, 2),
            "avg_pnl_pct": round(avg_pnl_pct, 2) if n_closed else None,
        },
        "rules_status": rules_status,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
    }
    with open(DOCS_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def main():
    run_date = date.today()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] running tracker for {run_date}")

    rules, sizing = load_rules()
    history = data_mod.fetch_all()

    signal_rows, triggers = evaluate_rules(rules, history, run_date)

    signals_df = load_csv(SIGNALS_LOG, SIGNAL_COLUMNS)
    signals_df = pd.concat([signals_df, pd.DataFrame(signal_rows)], ignore_index=True)
    signals_df.to_csv(SIGNALS_LOG, index=False)
    print(f"logged {len(signal_rows)} rule evaluations to {SIGNALS_LOG} "
          f"({sum(1 for r in signal_rows if r['triggered'])} triggered)")

    trades_df = load_csv(TRADES_LOG, TRADE_COLUMNS)
    trades_df = update_paper_trades(trades_df, triggers, run_date, sizing, rules)
    trades_df.to_csv(TRADES_LOG, index=False)

    open_count = (trades_df["status"] == "OPEN").sum() if not trades_df.empty else 0
    closed_today = trades_df[(trades_df["status"] == "CLOSED") &
                              (trades_df["exit_date"].astype(str) == str(run_date))] if not trades_df.empty else pd.DataFrame()
    deployed = trades_df.loc[trades_df["status"] == "OPEN", "position_value_inr"].sum() if not trades_df.empty else 0
    print(f"paper trades: {open_count} open (₹{deployed:.0f} of ₹{sizing['paper_capital_inr']} deployed), "
          f"{len(closed_today)} closed today")
    if not closed_today.empty:
        print(closed_today[["commodity", "stock", "signal", "entry_price", "exit_price",
                             "pnl_pct", "pnl_inr"]].to_string(index=False))

    if triggers:
        print("\nnew signals this run:")
        for commodity, stock, signal, entry_price, hold_days, move_strength in triggers:
            print(f"  {commodity} -> {stock}: {signal} @ {entry_price:.2f} "
                  f"(hold {hold_days}d, move_strength {move_strength:.2f}x threshold)")

    write_docs_data(trades_df, sizing, run_date, rules, signal_rows)
    print(f"wrote {DOCS_DATA_PATH}")


if __name__ == "__main__":
    main()
