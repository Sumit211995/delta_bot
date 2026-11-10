"""Signal generation. Indicators are computed once; signal_at(i) only uses data up to candle i
(a CLOSED candle), and entries happen on the next candle, so there is no look-ahead."""
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
    return d


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


def signal_at(d: pd.DataFrame, i: int, cfg):
    if i < cfg.ema_trend + 2 or i >= len(d):
        return None
    row, prev = d.iloc[i], d.iloc[i - 1]
    if np.isnan(row["atr"]) or np.isnan(row["rsi"]):
        return None
    up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
    dn = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]
    if up and row["close"] > row["ema_trend"] and row["rsi"] < 70:
        return Signal("buy", float(row["atr"]), "EMA cross up, above trend EMA", features(d, i, "buy"))
    if dn and row["close"] < row["ema_trend"] and row["rsi"] > 30:
        return Signal("sell", float(row["atr"]), "EMA cross down, below trend EMA", features(d, i, "sell"))
    return None


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
