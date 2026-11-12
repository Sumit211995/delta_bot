"""Walk-forward test of popular add-ons for the pullback strategy (BTC/ETH/SOL, Binance history 2019+).

Train = signals before 2024-01-01 (settings chosen here only); Test = 2024-01-01 onward (reported untouched).
Ideas: (1) BTC regime filter for ETH/SOL, (2) funding-rate extreme filter, (3) ATR% volatility filter,
(4) standalone Donchian breakout (ATR stop, 1:2). Uses backtest.simulate_trades (same fills/fees/slippage as the bot).
Run:  .venv/bin/python research/addon_filters.py   -> research/out/addon_filters.csv
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
from backtest import simulate_trades          # noqa: E402
from config import load_config                # noqa: E402
from strategy import add_indicators, signals_vectorized   # noqa: E402

SYMS = ["BTCUSD", "ETHUSD", "SOLUSD"]
SPLIT = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
SEC = {"1h": 3600, "4h": 14400, "1d": 86400}
BOOKS = [("4h", "fixed"), ("4h", "trail"), ("1d", "trail")]
OUT = ROOT / "research" / "out"


def candles(sym, tf):
    return pd.read_pickle(ROOT / "data" / "binance" / f"{sym.replace('USD', 'USDT')}_{tf}.pkl") \
        .drop_duplicates("time").sort_values("time").reset_index(drop=True)


def funding(sym):
    """Binance USDT-perp funding history (free, public), cached in data/binance/<SYM>_funding.pkl."""
    path = ROOT / "data" / "binance" / f"{sym.replace('USD', 'USDT')}_funding.pkl"
    old = pd.read_pickle(path) if path.exists() else pd.DataFrame(columns=["time", "rate"])
    cur = int(old["time"].max() * 1000) + 1 if len(old) else 1546300800000
    rows = []
    while True:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params=dict(symbol=sym.replace("USD", "USDT"), startTime=cur, limit=1000), timeout=30)
        r.raise_for_status()
        x = r.json()
        if not x:
            break
        rows += [(int(a["fundingTime"]) // 1000, float(a["fundingRate"])) for a in x]
        cur = int(x[-1]["fundingTime"]) + 1
        if len(x) < 1000:
            break
        time.sleep(0.3)
    df = pd.concat([old, pd.DataFrame(rows, columns=["time", "rate"])]).drop_duplicates("time").sort_values("time")
    df = df.reset_index(drop=True)
    df.to_pickle(path)
    return df


def asof(t_close, src_t, src_v):
    """Value of a series known at each candle close (last src_t <= t_close)."""
    idx = np.searchsorted(src_t, t_close, side="right") - 1
    out = np.full(len(t_close), np.nan)
    ok = idx >= 0
    out[ok] = src_v[idx[ok]]
    return out


def stats(trades, lo, hi):
    t = [x for x in trades if x["pnl"] is not None and lo <= x["signal_time"] < hi]
    if not t:
        return dict(n=0, win=0, pf=0, dd=0, sumr=0)
    t.sort(key=lambda x: x["exit_time"])
    r = np.array([x["r"] for x in t])
    gp, gl = r[r > 0].sum(), -r[r <= 0].sum()
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, eq])[1:] - eq)) if len(eq) else 0
    return dict(n=len(t), win=round(100 * (r > 0).mean(), 1), pf=round(gp / gl, 2) if gl else 99, dd=round(dd, 1),
                sumr=round(float(r.sum()), 1))


def run_book(cfg, data, sigs):
    allt = []
    for s in SYMS:
        allt += simulate_trades(data[s], cfg, capital=1000.0, sig=sigs[s])
    return stats(allt, 0, SPLIT), stats(allt, SPLIT, 2**40)


def donchian(d, n, trend):
    hh, ll = d["high"].rolling(n).max().shift(1), d["low"].rolling(n).min().shift(1)
    buy, sell = d["close"] > hh, d["close"] < ll
    if trend:
        buy &= d["close"] > d["ema_trend"]
        sell &= d["close"] < d["ema_trend"]
    s = np.where(buy, 1, np.where(sell, -1, 0))
    s[: max(n, 202)] = 0
    return s


def main():
    base = load_config()
    fund = {s: funding(s) for s in SYMS}
    rows = []
    for tf, mode in BOOKS:
        cfg = replace(base, resolution=tf, exit_mode=mode, strategy="pullback")
        per = SEC[tf]
        data = {s: add_indicators(candles(s, tf), cfg) for s in SYMS}
        tclose = {s: data[s]["time"].to_numpy() + per for s in SYMS}
        sig0 = {s: signals_vectorized(data[s], cfg) for s in SYMS}
        book = f"{tf}-{mode}"

        def add(idea, setting, sigs, c=cfg):
            tr, te = run_book(c, data, sigs)
            rows.append(dict(book=book, idea=idea, setting=setting, **{f"tr_{k}": v for k, v in tr.items()},
                             **{f"te_{k}": v for k, v in te.items()}))

        add("baseline", "pullback", sig0)
        # (1) BTC regime for ETH/SOL
        btc = candles("BTCUSD", tf)
        btc_d = candles("BTCUSD", "1d")
        for lab, src, span in [("same-tf EMA200", btc, 200), ("same-tf EMA50", btc, 50),
                               ("daily EMA200", btc_d, 200), ("daily EMA50", btc_d, 50)]:
            if tf == "1d" and lab.startswith("daily"):
                continue
            p = SEC["1d"] if src is btc_d else per
            up = (src["close"] > src["close"].ewm(span=span, adjust=False).mean()).astype(float).to_numpy()
            sigs = {}
            for s in SYMS:
                g = sig0[s].copy()
                if s != "BTCUSD":
                    reg = asof(tclose[s], src["time"].to_numpy() + p, up)
                    g[(g > 0) & (reg != 1)] = 0
                    g[(g < 0) & (reg != 0)] = 0
                sigs[s] = g
            add("btc_regime", lab, sigs)
        # (2) funding extreme
        for lab, q, n in [("pct q=.1 n=90", .1, 90), ("pct q=.2 n=90", .2, 90), ("pct q=.1 n=270", .1, 270),
                          ("pct q=.2 n=270", .2, 270), ("abs 0.05%", None, None)]:
            sigs = {}
            for s in SYMS:
                f = fund[s]
                fr = f["rate"]
                if q is None:
                    hi_, lo_ = (fr >= 0.0005).astype(float), (fr <= -0.0005).astype(float)
                else:
                    rk = fr.rolling(n, min_periods=n // 2).rank(pct=True)
                    hi_, lo_ = (rk >= 1 - q).astype(float), (rk <= q).astype(float)
                H = asof(tclose[s], f["time"].to_numpy(), hi_.to_numpy())
                L = asof(tclose[s], f["time"].to_numpy(), lo_.to_numpy())
                g = sig0[s].copy()
                g[(g > 0) & (H == 1)] = 0
                g[(g < 0) & (L == 1)] = 0
                sigs[s] = g
            add("funding", lab, sigs)
        # (3) ATR% volatility filter
        for lab, n, cut_lo, cut_hi in [("skip low n=100", 100, .2, None), ("skip high n=100", 100, None, .8),
                                       ("skip both n=100", 100, .2, .8), ("skip low n=500", 500, .2, None),
                                       ("skip high n=500", 500, None, .8), ("skip both n=500", 500, .2, .8)]:
            sigs = {}
            for s in SYMS:
                d = data[s]
                rk = (d["atr"] / d["close"]).rolling(n, min_periods=n // 2).rank(pct=True).to_numpy()
                g = sig0[s].copy()
                bad = np.zeros(len(g), bool)
                if cut_lo is not None:
                    bad |= rk < cut_lo
                if cut_hi is not None:
                    bad |= rk > cut_hi
                g[bad] = 0
                sigs[s] = g
            add("atr_vol", lab, sigs)
        # (4) Donchian breakout standalone, ATR stop x1.5, target 1:2 (fixed exit) and trailing variant via book mode
        dc = replace(cfg, reward_risk=2.0)
        for n in (20, 55):
            for tr_ in (True, False):
                add("donchian", f"n={n} trend={'EMA200' if tr_ else 'none'} 1:2",
                    {s: donchian(data[s], n, tr_) for s in SYMS}, dc)
        print(book, "done", flush=True)
    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "addon_filters.csv", index=False)
    pd.set_option("display.width", 250)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
