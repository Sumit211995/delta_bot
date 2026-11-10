"""Backtest the SAME strategy code the live bot uses.
Entry = open of the candle AFTER the signal candle. If stop and target are both inside one candle,
the STOP is assumed to hit first (pessimistic). Fees and slippage are applied on both sides.

  python backtest.py --days 365            # downloads public candles from Delta
  python backtest.py --days 1000 --symbol ETHUSD --resolution 4h
  python backtest.py --csv data.csv        # columns: time,open,high,low,close,volume
"""
import argparse
import dataclasses
import time

import numpy as np
import pandas as pd

from config import load_config
from risk import RiskManager
from strategy import add_indicators, levels, signal_at


def fetch_history(client, symbol, resolution, period_s, days):
    end, start = int(time.time()), int(time.time()) - days * 86400
    rows, cur = [], start
    while cur < end:                       # chunked: stay well under per-request candle limits
        chunk_end = min(cur + 1000 * period_s, end)
        rows += client.get_candles(symbol, resolution, cur, chunk_end)
        cur = chunk_end
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]].astype(float)


def run_backtest(df: pd.DataFrame, cfg, capital: float = 1000.0, contract_value: float = 1.0):
    """contract_value=1.0 -> sizes in base units (fine for research). Returns (trades_df, metrics)."""
    d = add_indicators(df.reset_index(drop=True), cfg)
    equity, peak, max_dd = capital, capital, 0.0
    trades, pos, pending = [], None, None
    slip = cfg.slippage_bps / 10000

    for j in range(len(d)):
        row = d.iloc[j]
        if pending is not None and pos is None:                      # enter at this candle's open
            side, atr, feats = pending
            entry = row["open"] * (1 + slip if side == "buy" else 1 - slip)
            sl, tp = levels(side, entry, atr, cfg)
            risk_amt = equity * cfg.risk_per_trade
            qty = risk_amt / abs(entry - sl)
            qty = min(qty, equity * cfg.max_leverage / entry)         # leverage cap
            pos = dict(side=side, entry=entry, sl=sl, tp=tp, qty=qty, risk=risk_amt, t=row["time"], f=feats)
            pending = None
        if pos is not None:                                           # manage exits on this candle
            long = pos["side"] == "buy"
            hit_sl = row["low"] <= pos["sl"] if long else row["high"] >= pos["sl"]
            hit_tp = row["high"] >= pos["tp"] if long else row["low"] <= pos["tp"]
            if hit_sl or hit_tp:
                exit_p = pos["sl"] if hit_sl else pos["tp"]           # stop first if both
                gross = (exit_p - pos["entry"]) * pos["qty"] * (1 if long else -1)
                fees = cfg.fee_rate * pos["qty"] * (pos["entry"] + exit_p)
                pnl = gross - fees
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak)
                trades.append(dict(entry_time=pos["t"], exit_time=row["time"], side=pos["side"],
                                   entry=pos["entry"], exit=exit_p, pnl=pnl, r=pnl / pos["risk"],
                                   win=int(pnl > 0), equity=equity, features=pos["f"]))
                pos = None
        if pos is None and pending is None:
            sig = signal_at(d, j, cfg)
            if sig is not None:
                pending = (sig.side, sig.atr, sig.features)

    t = pd.DataFrame(trades)
    bh = float(d["close"].iloc[-1] / d["close"].iloc[0] - 1)
    if t.empty:
        return t, dict(trades=0, buy_hold=bh)
    gp, gl = t.loc[t.pnl > 0, "pnl"].sum(), -t.loc[t.pnl <= 0, "pnl"].sum()
    after_tax = float((t.pnl.where(t.pnl <= 0, t.pnl * 0.7)).sum())  # pessimistic: 30% on each win, no loss offset
    return t, dict(trades=len(t), win_rate=float(t.win.mean()), avg_r=float(t.r.mean()),
                   profit_factor=float(gp / gl) if gl > 0 else float("inf"),
                   total_return=float(equity / capital - 1), max_drawdown=float(max_dd),
                   after_30pct_tax_return=float(after_tax / capital), buy_hold=bh)


def report(m: dict) -> str:
    if m.get("trades", 0) == 0:
        return f"No trades. Buy&hold: {m.get('buy_hold', 0):.1%}"
    return (f"trades={m['trades']}  win_rate={m['win_rate']:.1%}  avg_R={m['avg_r']:.2f}  "
            f"profit_factor={m['profit_factor']:.2f}\ntotal_return={m['total_return']:.1%}  "
            f"max_drawdown={m['max_drawdown']:.1%}  buy&hold={m['buy_hold']:.1%}\n"
            f"pessimistic after-tax return (30% on wins, no loss offset)={m['after_30pct_tax_return']:.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--csv")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--symbol")
    ap.add_argument("--resolution")
    ap.add_argument("--strategy")
    a = ap.parse_args()
    cfg = load_config()
    over = {k: v for k, v in dict(symbol=a.symbol and a.symbol.upper(), resolution=a.resolution,
                                    strategy=a.strategy).items() if v}
    cfg = dataclasses.replace(cfg, **over)
    if a.csv:
        data = pd.read_csv(a.csv)
    else:
        from delta_client import DeltaClient
        data = fetch_history(DeltaClient(cfg.base_url), cfg.symbol, cfg.resolution, cfg.period_seconds, a.days)
    print(f"{len(data)} candles, {cfg.symbol} {cfg.resolution} strategy={cfg.strategy} "
          f"SL={cfg.atr_stop_mult}xATR target={cfg.reward_risk}R fee={cfg.fee_rate}")
    trades, metrics = run_backtest(data, cfg, a.capital)
    print(report(metrics))
    if len(trades):
        y = pd.to_datetime(trades.exit_time, unit="s").dt.year
        print("\nper year:")
        for yr, g in trades.groupby(y):
            gl = -g.pnl[g.pnl <= 0].sum()
            pf = g.pnl[g.pnl > 0].sum() / gl if gl else float("inf")
            print(f"  {yr}: trades={len(g)} win_rate={g.win.mean():.0%} profit_factor={pf:.2f} pnl={g.pnl.sum():+.1f}")
    print("\nRULE: do not go live unless this holds on data you did NOT tune on (see README: walk-forward).")
