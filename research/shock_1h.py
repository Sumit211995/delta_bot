"""Big-news (shock) candles on 1-HOUR candles: does a shock help a trade on 1h? (BTC/ETH/SOL, Binance 2019+)
Same rule family as research/bigmoney_news.py (body >= k x previous ATR on >= 3x 20-candle volume), same simulator,
fees, slippage and walk-forward split (choose before 2024, judge 2024 -> Nov 2026). Follow vs fade, with/without the
EMA200 trend, fixed 1:2 / 1:3 targets or the trailing stop.  Run: .venv/bin/python research/shock_1h.py"""
import sys
from dataclasses import replace
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "research"))
from addon_filters import SYMS, candles, run_book            # noqa: E402
from bigmoney_news import shocks                            # noqa: E402
from config import load_config                              # noqa: E402
from strategy import add_indicators                         # noqa: E402

base = load_config()
rows = []
for mode in ("fixed", "trail"):
    cfg = replace(base, resolution="1h", exit_mode=mode, strategy="pullback")
    data = {s: add_indicators(candles(s, "1h"), cfg) for s in SYMS}
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
                    a, b = run_book(c, data, sigs)
                    rows.append(dict(setting=f"1h {side} k={k} trend={'EMA200' if trend else 'none'} " + (f"1:{rr:g}" if rr else "trail"),
                                     **{f"tr_{x}": a.get(x) for x in ("n", "win", "pf", "sumr", "dd")},
                                     **{f"te_{x}": b.get(x) for x in ("n", "win", "pf", "sumr", "dd")}))
                    r = rows[-1]; print(f"{r['setting']:<42} train n {r['tr_n']} win {r['tr_win']} pf {r['tr_pf']} R {r['tr_sumr']} dd {r['tr_dd']} | test n {r['te_n']} win {r['te_win']} pf {r['te_pf']} R {r['te_sumr']} dd {r['te_dd']}", flush=True)
(ROOT / "research" / "out").mkdir(exist_ok=True)
pd.DataFrame(rows).to_csv(ROOT / "research" / "out" / "shock_1h.csv", index=False)
