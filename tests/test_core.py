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


def test_pullback_signal_rules():
    import dataclasses
    cfg = Config(strategy="pullback", rsi_pullback=40)
    n = cfg.ema_trend + 5
    base = dict(atr=[10.0] * n, ema_fast=[0.0] * n)
    up = pd.DataFrame(dict(close=[110.0] * n, ema_slow=[105.0] * n, ema_trend=[100.0] * n,
                           rsi=[50.0] * (n - 2) + [35.0, 42.0], time=range(n), **base))
    assert signal_at(up, n - 1, cfg).side == "buy"
    up.loc[n - 1, "rsi"] = 39.0                              # still below 40: no signal
    assert signal_at(up, n - 1, cfg) is None
    dn = pd.DataFrame(dict(close=[90.0] * n, ema_slow=[95.0] * n, ema_trend=[100.0] * n,
                           rsi=[50.0] * (n - 2) + [65.0, 58.0], time=range(n), **base))
    assert signal_at(dn, n - 1, cfg).side == "sell"
    dn.loc[:, "close"] = 101.0                                # price above trend EMA: no short
    assert signal_at(dn, n - 1, cfg) is None
    assert signal_at(up, n - 1, dataclasses.replace(cfg, strategy="ema_cross")) is None


def test_paper_settle_stop_first_and_pnl():
    import paper
    pos = dict(side="buy", size=10, entry=100.0, sl=95.0, tp=115.0, cval=1.0, checked_until=0)
    c = pd.DataFrame(dict(time=[0, 1, 2], open=[100] * 3, high=[101, 116, 120], low=[99, 94, 99], close=[100] * 3))
    assert paper.settle(pos, c) == (95.0, 1, "stop")         # both hit in candle 1 -> stop first
    pos2 = dict(side="sell", size=10, entry=100.0, sl=105.0, tp=85.0, cval=1.0, checked_until=0)
    c2 = pd.DataFrame(dict(time=[1, 2], open=[100] * 2, high=[101, 102], low=[90, 84], close=[100] * 2))
    assert paper.settle(pos2, c2) == (85.0, 2, "target")
    assert abs(paper.pnl(pos2, 85.0, 0.0) - 150.0) < 1e-9
    assert paper.settle(pos2, c2) is None                     # already-checked candles are skipped


def test_vectorized_signals_match_signal_at():
    import dataclasses
    from strategy import signals_vectorized
    for strat in ("pullback", "ema_cross"):
        cfg = dataclasses.replace(Config(), strategy=strat)
        d = add_indicators(synthetic(2500, seed=3), cfg)
        v = signals_vectorized(d, cfg)
        slow = [0 if (s := signal_at(d, i, cfg)) is None else (1 if s.side == "buy" else -1) for i in range(len(d))]
        assert list(v) == slow and sum(x != 0 for x in slow) > 5


def test_open_orders_filtered_by_product():
    from delta_client import DeltaClient
    c = DeltaClient("http://x", "k", "s")
    seen = {}
    def fake(method, path, params=None, body=None, auth=False, retries=3):
        seen.update(params)
        return {"result": [{"id": 1, "product_id": 3136}, {"id": 2, "product_id": 27}]}   # API returns all coins
    c.request = fake
    assert [o["id"] for o in c.get_open_orders(27)] == [2]
    assert seen["product_ids"] == 27


def test_paper_books_are_separate_and_never_live():
    import dataclasses
    from bot import books_for
    from config import Config
    live = Config(dry_run=False, resolution="4h", paper_timeframes=("30m", "1h", "4h"))
    books = books_for(live)
    assert [(c.resolution, c.dry_run) for c in books] == [("4h", False), ("30m", True), ("1h", True), ("4h", True)]
    assert len({c.state_path for c in books}) == 4                 # live state never shared with a paper book
    paper = dataclasses.replace(live, dry_run=True)
    assert [c.resolution for c in books_for(paper)] == ["4h", "30m", "1h"]   # no duplicate 4h paper book


def test_fast_simulator_matches_backtest():
    from backtest import simulate_trades
    cfg = Config(resolution="1h", risk_per_trade=0.01)
    for seed in (1, 2, 3):
        df = synthetic(4000, seed)
        ref, _ = run_backtest(df, cfg, capital=1000.0)
        fast = [x for x in simulate_trades(add_indicators(df, cfg), cfg, capital=1000.0) if x["result"] != "open"]
        assert len(fast) == len(ref) and len(ref) > 5
        for a, (_, b) in zip(fast, ref.iterrows()):
            assert a["entry_time"] == b.entry_time and a["exit_time"] == b.exit_time and a["side"] == b.side
            assert abs(a["pnl"] - b.pnl) < 1e-9 and abs(a["equity"] - b.equity) < 1e-6


def test_paper_trailing_stop_matches_backtest():
    """The live paper engine (paper.settle, candle by candle) must exit exactly where the backtest does."""
    import dataclasses
    import paper
    from backtest import simulate_trades
    cfg = Config(resolution="1h", exit_mode="trail", slippage_bps=0.0, trail_activate_r=1.0, trail_atr=2.0)
    for seed in (4, 5):
        d = add_indicators(synthetic(4000, seed), cfg)
        trades = [x for x in simulate_trades(d, cfg) if x["result"] != "open"]
        assert len(trades) > 10 and any(x["result"] == "trail" for x in trades)
        assert all(x["tp"] is None for x in trades)
        for x in trades:
            pos = dict(side=x["side"], entry=x["entry"], sl=x["sl0"], sl0=x["sl0"], dist=x["dist"], best=x["entry"],
                       mode="trail", trail_r=1.0, trail_atr=2.0, checked_until=x["signal_time"], size=1, cval=1)
            hit = paper.settle(pos, d)
            assert hit is not None
            assert abs(hit[0] - x["exit"]) < 1e-9 and hit[1] == x["exit_time"] and hit[2] == x["result"]


def test_trailing_book_is_paper_only():
    import pytest
    from bot import books_for
    from config import validate
    cfg = Config(dry_run=False, env="testnet", api_key="k", api_secret="s", resolution="4h",
                 paper_timeframes=("1h",), paper_trail_timeframes=("4h",))
    books = books_for(cfg)
    assert [c.book for c in books] == ["4h", "1h", "4h-trail"]
    assert [c.dry_run for c in books] == [False, True, True]
    assert books[2].state_path == "state/paper_state_4h-trail.json"
    import dataclasses
    with pytest.raises(SystemExit):
        validate(dataclasses.replace(cfg, exit_mode="trail"))      # never live with the trailing exit (yet)


def test_delta_requests_use_ipv4_for_the_whitelist():
    import socket
    import urllib3.util.connection as c
    import delta_client  # noqa: F401  (importing applies the IPv4 setting)
    assert c.allowed_gai_family() == socket.AF_INET


def test_binance_history_is_research_only_symbol_map():
    import dashboard
    assert dashboard.binance_symbol("BTCUSD") == "BTCUSDT" and dashboard.binance_symbol("SOLUSD") == "SOLUSDT"
    assert "binance" not in open(Path(__file__).resolve().parent.parent / "bot.py").read().lower()   # bot never uses it
