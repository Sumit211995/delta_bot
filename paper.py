"""Paper (DRY_RUN) position simulation: same rules as the backtest (stop checked first inside a candle)."""
from typing import Optional, Tuple

import pandas as pd


def settle(pos: dict, candles: pd.DataFrame) -> Optional[Tuple[float, int, str]]:
    """Check closed candles newer than pos['checked_until'] for a stop / target hit.
    Returns (exit_price, candle_time, 'stop'|'target') or None. Updates pos['checked_until']."""
    long = pos["side"] == "buy"
    for _, c in candles[candles["time"] > pos["checked_until"]].iterrows():
        pos["checked_until"] = int(c["time"])
        hit_sl = c["low"] <= pos["sl"] if long else c["high"] >= pos["sl"]
        hit_tp = c["high"] >= pos["tp"] if long else c["low"] <= pos["tp"]
        if hit_sl:
            return float(pos["sl"]), int(c["time"]), "stop"
        if hit_tp:
            return float(pos["tp"]), int(c["time"]), "target"
    return None


def pnl(pos: dict, exit_price: float, fee_rate: float) -> float:
    direction = 1 if pos["side"] == "buy" else -1
    qty = pos["size"] * pos["cval"]
    return (exit_price - pos["entry"]) * direction * qty - fee_rate * qty * (pos["entry"] + exit_price)
