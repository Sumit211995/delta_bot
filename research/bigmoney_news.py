"""Walk-forward test of "big money" and "big news" ideas for the pullback strategy (BTC/ETH/SOL, Binance 2019+).

Big money = taker flow: Binance futures klines carry the taker BUY volume (aggressive buyers), so
taker ratio = taker buy volume / volume, and CVD (cumulative volume delta) = Σ(2·takerBuy − volume).
Big news  = shock candles: |close − open| ≥ k × ATR with volume ≥ 3× its 20-candle average.

Train = signals before 2024-01-01 (settings chosen here); Test = 2024-01-01 → Nov 2026 (untouched).
Books: 4h fixed (1:3), 4h trail, 1d trail (1d-trail keeps its adopted high-volatility filter, as in the bot).
Uses backtest.simulate_trades (same fills, fees, slippage and stops as the bot), results in R.
Open interest / long-short ratio history is only ~30 days on Binance, so they are not tested.
Run:  .venv/bin/python research/bigmoney_news.py   -> research/out/bigmoney_news.csv
"""
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
from addon_filters import BOOKS, SEC, SPLIT, SYMS, candles, run_book   # noqa: E402
from config import load_config                                         # noqa: E402
from strategy import add_indicators, signals_vectorized                # noqa: E402

OUT = ROOT / "research" / "out"
CACHE = ROOT / "data" / "binance"


def taker(sym, tf):
    """Binance USDT-M klines with the taker-buy base volume (column 9), cached in data/binance/<SYM>_<tf>_taker.pkl."""
    bs = sym.replace("USD", "USDT")
    path = CACHE / f"{bs}_{tf}_taker.pkl"
    old = pd.read_pickle(path) if path.exists() else pd.DataFrame(columns=["time", "volume", "taker_buy"])
    cur = (int(old["time"].max()) + SEC[tf]) * 1000 if len(old) else 1567296000000   # 2019-09-01
    rows = []
    while True:
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params=dict(symbol=bs, interval=tf, startTime=cur, limit=1500), timeout=30)
        r.raise_for_status()
        x = r.json()
        if not x:
            break
        rows += [(int(a[0]) // 1000, float(a[5]), float(a[9])) for a in x]
        cur = int(x[-1][0]) + SEC[tf] * 1000
        if len(x) < 1500:
            break
        time.sleep(0.35)
    df = pd.concat([old, pd.DataFrame(rows, columns=["time", "volume", "taker_buy"])])
    df = df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_pickle(tmp)
    tmp.replace(path)
    return df


def join_taker(d, tk):
    m = d[["time"]].merge(tk[["time", "volume", "taker_buy"]].rename(columns={"volume": "tvol"}), on="time", how="left")
    ratio = (m["taker_buy"] / m["tvol"]).to_numpy()
    delta = (2 * m["taker_buy"] - m["tvol"]).to_numpy()
    return ratio, delta


def shocks(d, k, vmult=3.0):
    """+1 / −1 on a shock candle (body ≥ k·ATR of the previous candle, volume ≥ vmult × 20-candle average)."""
    body = (d["close"] - d["open"]).to_numpy()
    atr_prev = d["atr"].shift(1).to_numpy()
    vavg = d["volume"].rolling(20).mean().shift(1).to_numpy()
    big = (np.abs(body) >= k * atr_prev) & (d["volume"].to_numpy() >= vmult * vavg)
    return np.where(big, np.sign(body), 0).astype(int)


def main():
    base = load_config()
    rows = []
    for tf, mode in BOOKS:
        cfg = replace(base, resolution=tf, exit_mode=mode, strategy="pullback")
        data = {s: add_indicators(candles(s, tf), cfg) for s in SYMS}
        sig0 = {s: signals_vectorized(data[s], cfg) for s in SYMS}
        tk = {s: join_taker(data[s], taker(s, tf)) for s in SYMS}
        book = f"{tf}-{mode}"

        def add(idea, setting, sigs, c=cfg):
            tr, te = run_book(c, data, sigs)
            rows.append(dict(book=book, idea=idea, setting=setting, **{f"tr_{k}": v for k, v in tr.items()},
                             **{f"te_{k}": v for k, v in te.items()}))

        add("baseline", "pullback", sig0)
        cov = {s: float(np.isfinite(tk[s][0]).mean()) for s in SYMS}
        print(book, "taker coverage", {s: round(v, 3) for s, v in cov.items()}, flush=True)

        # (1a) taker ratio over the last N candles vs its own rolling median: longs need buyers in control, shorts sellers
        for n in (3, 6, 12):
            sigs = {}
            for s in SYMS:
                ratio = pd.Series(tk[s][0])
                avg = ratio.rolling(n, min_periods=n).mean()
                med = avg.rolling(500, min_periods=100).median()
                g = sig0[s].copy()
                ok_l = (avg > med).to_numpy()
                ok_s = (avg < med).to_numpy()
                g[(g > 0) & ~ok_l] = 0
                g[(g < 0) & ~ok_s] = 0
                sigs[s] = g
            add("taker_ratio", f"agree n={n} vs 500-median", sigs)
        # (1b) skip signals AGAINST an extreme taker imbalance (top / bottom 10% of 500 candles, n=6)
        for q in (0.1, 0.2):
            sigs = {}
            for s in SYMS:
                avg = pd.Series(tk[s][0]).rolling(6, min_periods=6).mean()
                rk = avg.rolling(500, min_periods=100).rank(pct=True).to_numpy()
                g = sig0[s].copy()
                g[(g > 0) & (rk <= q)] = 0          # long while sellers dominate
                g[(g < 0) & (rk >= 1 - q)] = 0      # short while buyers dominate
                sigs[s] = g
            add("taker_extreme", f"skip against top/bottom {int(q * 100)}% (n=6)", sigs)
        # (1c) CVD slope agreement (Σ delta over the last N candles has the trade's sign)
        for n in (6, 12, 24):
            sigs = {}
            for s in SYMS:
                cvd = pd.Series(tk[s][1]).rolling(n, min_periods=n).sum().to_numpy()
                g = sig0[s].copy()
                g[(g > 0) & ~(cvd > 0)] = 0
                g[(g < 0) & ~(cvd < 0)] = 0
                sigs[s] = g
            add("cvd", f"agree sum {n} candles", sigs)

        # (2a) shock candles as a FILTER on pullback signals
        for k in (2.5, 3.0, 4.0):
            sh = {s: shocks(data[s], k) for s in SYMS}
            for m in (3, 6):
                sigs = {}
                for s in SYMS:
                    recent = pd.Series(np.abs(sh[s])).rolling(m, min_periods=1).max().to_numpy() > 0
                    g = sig0[s].copy()
                    g[recent] = 0
                    sigs[s] = g
                add("shock_filter", f"skip {m} candles after shock k={k}", sigs)
            sigs = {}
            for s in SYMS:
                last = pd.Series(np.where(sh[s] != 0, sh[s], np.nan)).ffill(limit=12).fillna(0).to_numpy()
                g = sig0[s].copy()
                g[(g != 0) & (last != 0) & (np.sign(g) != last)] = 0     # only in the direction of a recent shock
                sigs[s] = g
            add("shock_filter", f"only with last shock (12 candles) k={k}", sigs)

        # (2b) shock candles as a STAND-ALONE trade: follow / fade, with/without the EMA200 trend filter
        for k in (2.5, 3.0, 4.0):
            for side in ("follow", "fade"):
                for trend in (False, True):
                    sigs = {}
                    for s in SYMS:
                        g = shocks(data[s], k) * (1 if side == "follow" else -1)
                        if trend:
                            up = (data[s]["close"] > data[s]["ema_trend"]).to_numpy()
                            g[(g > 0) & ~up] = 0
                            g[(g < 0) & up] = 0
                        g[:202] = 0
                        sigs[s] = g
                    for rr in ((2.0, 3.0) if mode == "fixed" else (None,)):
                        c = replace(cfg, reward_risk=rr) if rr else cfg
                        add("shock_trade", f"{side} k={k} trend={'EMA200' if trend else 'none'}"
                            + (f" 1:{rr:g}" if rr else " trail"), sigs, c)
        print(book, "done", flush=True)
    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "bigmoney_news.csv", index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 400)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
