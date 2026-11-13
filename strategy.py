"""Signal generation. Indicators are computed once; signal_at(i) only uses data up to candle i
(a CLOSED candle), and entries happen on the next candle, so there is no look-ahead.

Strategies (STRATEGY in .env):
  pullback  (default) trend pullback: in an uptrend (EMA50 > EMA200 and close > EMA200) buy when RSI dips
            below RSI_PULLBACK (40) and crosses back above it. Mirror image for shorts (RSI 60).
            Walk-forward tested on BTC + ETH 4h, 2023-2026: small but positive edge on both coins.
  ema_cross the original EMA20/50 crossover with EMA200 trend filter (loses money on 1h in recent years).

High-volatility filter (VOL_FILTER_BOOKS, default 1d-trail): skip a signal when ATR/close is in the top 20% of the
last 500 candles. Chosen on 2019-2023 Binance data, checked on 2024-2026 (research/addon_filters.py): 1d-trail
PF 3.22 -> 4.28 (train) and 1.57 -> 1.69 (test), max drawdown 12.5R -> 8.1R in the test years.
"""
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd


@dataclass
class Signal:
    side: str       # "buy" | "sell"
    atr: float
    reason: str
    features: list


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    d["ema_fast"] = c.ewm(span=cfg.ema_fast, adjust=False).mean()
    d["ema_slow"] = c.ewm(span=cfg.ema_slow, adjust=False).mean()
    d["ema_trend"] = c.ewm(span=cfg.ema_trend, adjust=False).mean()
    pc = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / cfg.atr_period, adjust=False).mean()
    diff = c.diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    if vol_filter_on(cfg):
        n = cfg.vol_filter_n
        d["atr_rank"] = (d["atr"] / c).rolling(n, min_periods=n // 2).rank(pct=True)
    if "volume" in d:                                           # shock candles ("big news"): see _shock
        d["atr_prev"] = d["atr"].shift(1)
        d["vol_avg20"] = d["volume"].rolling(20).mean().shift(1)
    return d


def vol_filter_on(cfg) -> bool:
    return cfg.book in getattr(cfg, "vol_filter_books", ())


def vol_blocked(row, cfg) -> bool:
    """True when this candle's volatility is too high for this book (unknown rank = not blocked)."""
    return vol_filter_on(cfg) and bool(row.get("atr_rank", np.nan) > cfg.vol_filter_max)


def features(d: pd.DataFrame, i: int, side: str) -> list:
    """Feature vector shared by live trading and train_model.py (keep them identical)."""
    r = d.iloc[i]
    atr = r["atr"] if r["atr"] else np.nan
    return [
        float(d["close"].iloc[i] / d["close"].iloc[i - 1] - 1),
        float(d["close"].iloc[i] / d["close"].iloc[max(i - 4, 0)] - 1),
        float(atr / r["close"]),
        float(r["rsi"]),
        float((r["close"] - r["ema_trend"]) / atr),
        float((r["ema_fast"] - r["ema_slow"]) / atr),
        1.0 if side == "buy" else -1.0,
    ]


@lru_cache(maxsize=2)
def _load_model(path: str):
    import joblib
    return joblib.load(path)


def ml_pass(sig: Signal, cfg) -> bool:
    """Optional filter: trade only if the trained model's win probability >= threshold."""
    if not cfg.ml_model_path:
        return True
    p = float(_load_model(cfg.ml_model_path).predict_proba([sig.features])[0][1])
    return p >= cfg.ml_threshold


def _ema_cross(d, i, row, prev, cfg):
    up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
    dn = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]
    if up and row["close"] > row["ema_trend"] and row["rsi"] < 70:
        return "buy", "EMA cross up, above trend EMA"
    if dn and row["close"] < row["ema_trend"] and row["rsi"] > 30:
        return "sell", "EMA cross down, below trend EMA"
    return None, ""


def _pullback(d, i, row, prev, cfg):
    lo, hi = cfg.rsi_pullback, 100 - cfg.rsi_pullback
    trend_up = row["ema_slow"] > row["ema_trend"] and row["close"] > row["ema_trend"]
    trend_dn = row["ema_slow"] < row["ema_trend"] and row["close"] < row["ema_trend"]
    if trend_up and prev["rsi"] < lo <= row["rsi"]:
        return "buy", f"uptrend pullback: RSI back above {lo:g}"
    if trend_dn and prev["rsi"] > hi >= row["rsi"]:
        return "sell", f"downtrend pullback: RSI back below {hi:g}"
    return None, ""


def _shock(d, i, row, prev, cfg):
    """Shock-follow ("big news" candle): the closed candle's body is >= SHOCK_K x ATR of the candle before, on volume
    >= SHOCK_VOL x its 20-candle average, in the direction of the EMA200 trend. Paper only, trailing stop.
    Research (research/bigmoney_news.py, 4h trail, k=3): Binance 2019-23 PF 3.17 (71 trades), 2024-26 PF 2.93 (53);
    Delta's own candles Apr 2024 - Nov 2026 PF 2.58 (26 trades)."""
    atr_p, vavg = row.get("atr_prev", np.nan), row.get("vol_avg20", np.nan)
    if np.isnan(atr_p) or np.isnan(vavg) or vavg <= 0:
        return None, ""
    body = row["close"] - row["open"]
    if abs(body) < cfg.shock_k * atr_p or row["volume"] < cfg.shock_vol * vavg:
        return None, ""
    x = f"shock candle {body / atr_p:+.1f} ATR on {row['volume'] / vavg:.1f}x volume"
    if body > 0 and row["close"] > row["ema_trend"]:
        return "buy", x + ", above EMA200"
    if body < 0 and row["close"] < row["ema_trend"]:
        return "sell", x + ", below EMA200"
    return None, ""


STRATEGIES = {"pullback": _pullback, "ema_cross": _ema_cross, "shock": _shock}


def signal_at(d: pd.DataFrame, i: int, cfg):
    if i < cfg.ema_trend + 2 or i >= len(d):
        return None
    row, prev = d.iloc[i], d.iloc[i - 1]
    if np.isnan(row["atr"]) or np.isnan(row["rsi"]) or np.isnan(prev["rsi"]):
        return None
    side, reason = STRATEGIES[cfg.strategy](d, i, row, prev, cfg)
    if side is None or vol_blocked(row, cfg):
        return None
    return Signal(side, float(row["atr"]), reason, features(d, i, side))


def round_tick(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    decimals = max(0, -int(np.floor(np.log10(tick)))) if tick < 1 else 0
    return round(round(x / tick) * tick, decimals)


def levels(side: str, entry: float, atr: float, cfg, tick: float = 0.0):
    dist = atr * cfg.atr_stop_mult
    if side == "buy":
        sl, tp = entry - dist, entry + dist * cfg.reward_risk
    else:
        sl, tp = entry + dist, entry - dist * cfg.reward_risk
    return round_tick(sl, tick), round_tick(tp, tick)


def trail_stop(side: str, entry: float, dist: float, best: float, sl: float, atr: float,
               activate_r: float, trail_atr: float) -> float:
    """Trailing-stop rule (paper bot + backtest share it). Called at a candle CLOSE with the best close so far:
    once the best close is >= activate_r x the initial stop distance in profit, the stop follows trail_atr x ATR
    behind it. The stop only ever moves in the trade's favour."""
    d = 1 if side == "buy" else -1
    if (best - entry) * d >= activate_r * dist:
        new = best - d * trail_atr * atr
        sl = max(sl, new) if d > 0 else min(sl, new)
    return sl


def signals_vectorized(d: pd.DataFrame, cfg) -> np.ndarray:
    """Same rules as signal_at() for every row at once (+1 buy, -1 sell, 0 none). Used by the dashboard to draw
    years of past signals quickly. tests/test_core.py checks it matches signal_at() exactly."""
    c, fast, slow, trend, rsi = d["close"], d["ema_fast"], d["ema_slow"], d["ema_trend"], d["rsi"]
    prsi = rsi.shift(1)
    if cfg.strategy == "pullback":
        lo, hi = cfg.rsi_pullback, 100 - cfg.rsi_pullback
        buy = (slow > trend) & (c > trend) & (prsi < lo) & (rsi >= lo)
        sell = (slow < trend) & (c < trend) & (prsi > hi) & (rsi <= hi)
    elif cfg.strategy == "shock":
        body = c - d["open"]
        big = (body.abs() >= cfg.shock_k * d["atr_prev"]) & (d["volume"] >= cfg.shock_vol * d["vol_avg20"]) \
            & (d["vol_avg20"] > 0)
        buy = big & (body > 0) & (c > trend)
        sell = big & (body < 0) & (c < trend)
    else:
        up = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
        dn = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
        buy = up & (c > trend) & (rsi < 70)
        sell = dn & (c < trend) & (rsi > 30)
    valid = d["atr"].notna() & rsi.notna() & prsi.notna()
    valid &= pd.Series(np.arange(len(d)) >= cfg.ema_trend + 2, index=d.index)
    if vol_filter_on(cfg):
        valid &= ~(d["atr_rank"] > cfg.vol_filter_max)
    out = np.zeros(len(d), dtype=int)
    out[(buy & valid).to_numpy()] = 1
    out[(sell & valid & ~buy).to_numpy()] = -1
    return out
