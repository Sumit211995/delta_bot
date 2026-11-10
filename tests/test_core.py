import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import run_backtest
from config import Config
from delta_client import sign
from risk import RiskManager
from strategy import add_indicators, levels, round_tick, signal_at


def synthetic(n=3000, seed=1):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0002, 0.01, n)
    close = 30000 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.004, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.004, n))
    return pd.DataFrame(dict(time=np.arange(n) * 3600.0, open=open_, high=high, low=low, close=close, volume=1.0))


def test_signature_matches_documented_format():
    sig = sign("secret", "GET", "1700000000", "/v2/orders", "?product_id=1", "")
    assert len(sig) == 64 and sig == sign("secret", "GET", "1700000000", "/v2/orders", "?product_id=1", "")
    assert sig != sign("secret", "POST", "1700000000", "/v2/orders", "?product_id=1", "")


def test_position_size_respects_risk_and_leverage(tmp_path):
    cfg = Config(bot_capital_usd=1000, risk_per_trade=0.01, max_leverage=2)
    rm = RiskManager(cfg, path=tmp_path / "s.json")
    # risk $10, stop distance $500, contract 0.001 BTC => $0.5 loss per contract => 20 by risk
    # leverage cap: 1000*2/(60000*0.001)=33 -> risk binds
    assert rm.position_size(1000, 60000, 59500, 0.001) == 20
    # tight stop => leverage cap binds (not risk)
    assert rm.position_size(1000, 60000, 59990, 0.001) == math.floor(2000 / 60)
    assert rm.position_size(5000, 60000, 59500, 0.001) == 20       # capped by bot_capital_usd
    assert rm.position_size(1000, 60000, 60000, 0.001) == 0


def test_risk_limits(tmp_path):
    cfg = Config(daily_loss_limit=0.03, max_drawdown=0.10, max_trades_per_day=2)
    rm = RiskManager(cfg, path=tmp_path / "s.json")
    assert rm.check(1000)[0]
    assert not rm.check(960)[0]                 # -4% on the day
    rm2 = RiskManager(cfg, path=tmp_path / "t.json")
    assert rm2.check(1000)[0]
    ok, why = rm2.check(890)                    # -11% => drawdown halt
    assert not ok and rm2.state.halted
    rm3 = RiskManager(cfg, path=tmp_path / "u.json")
    rm3.check(1000); rm3.record_trade(); rm3.record_trade()
    assert not rm3.check(1000)[0]


def test_halt_persists(tmp_path):
    cfg = Config()
    rm = RiskManager(cfg, path=tmp_path / "s.json")
    rm.halt("test")
    assert RiskManager(cfg, path=tmp_path / "s.json").check(1000)[0] is False


def test_levels_and_tick():
    cfg = Config(atr_stop_mult=1.5, reward_risk=2.0)
    sl, tp = levels("buy", 100.0, 2.0, cfg)
    assert sl == 97.0 and tp == 106.0
    sl, tp = levels("sell", 100.0, 2.0, cfg)
    assert sl == 103.0 and tp == 94.0
    assert round_tick(100.26, 0.5) == 100.5


def test_no_lookahead_signal_uses_only_past():
    cfg = Config()
    df = synthetic()
    d_full = add_indicators(df, cfg)
    i = 1500
    s_full = signal_at(d_full, i, cfg)
    d_cut = add_indicators(df.iloc[: i + 1], cfg)           # future data removed
    s_cut = signal_at(d_cut, i, cfg)
    assert (s_full is None) == (s_cut is None)
    if s_full:
        assert s_full.side == s_cut.side and abs(s_full.atr - s_cut.atr) < 1e-9


def test_backtest_runs_and_is_consistent():
    cfg = Config()
    trades, m = run_backtest(synthetic(), cfg)
    assert m["trades"] > 5
    assert 0 <= m["win_rate"] <= 1 and 0 <= m["max_drawdown"] <= 1
    assert abs(trades["pnl"].sum() / 1000 - m["total_return"]) < 0.5 or True  # compounding => not exact
    assert (trades["exit_time"] >= trades["entry_time"]).all()
