import dataclasses

import numpy as np
import pandas as pd

import bot
from config import load_config
from strategy import add_indicators, signal_at, signals_vectorized


def _cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "_load_env", lambda: None)
    for k in ("PAPER_SHOCK_TIMEFRAMES", "SHOCK_K", "SHOCK_VOL", "PAPER_TIMEFRAMES", "PAPER_TRAIL_TIMEFRAMES"):
        monkeypatch.delenv(k, raising=False)
    return load_config()


def _frame(n=320, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0.05, 0.5, n))
    o = np.r_[c[0], c[:-1]]
    h, l = np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3
    v = np.full(n, 1000.0)
    i = n - 5                                                   # one big up candle on 5x volume, in an uptrend
    c[i] = o[i] + 8
    h[i] = c[i] + 0.2
    v[i] = 5000.0
    return pd.DataFrame(dict(time=np.arange(n) * 14400, open=o, high=h, low=l, close=c, volume=v)), i


def test_shock_book_is_paper_trail_and_named(monkeypatch):
    cfg = _cfg(monkeypatch)
    books = bot.books_for(dataclasses.replace(cfg, paper_timeframes=(), paper_trail_timeframes=()))
    sh = [c for c in books if c.strategy == "shock"]
    assert [c.book for c in sh] == ["4h-shock-trail", "1h-shock-trail"]
    assert all(c.exit_mode == "trail" and c.dry_run for c in sh)
    assert books[0].strategy == "pullback" and books[0].book == cfg.book       # main book unchanged


def test_shock_signal_live_matches_vectorized(monkeypatch):
    cfg = dataclasses.replace(_cfg(monkeypatch), strategy="shock", exit_mode="trail")
    df, i = _frame()
    d = add_indicators(df, cfg)
    vec = signals_vectorized(d, cfg)
    live = np.array([0] * len(d))
    for j in range(len(d)):
        s = signal_at(d, j, cfg)
        live[j] = 0 if s is None else (1 if s.side == "buy" else -1)
    assert (vec == live).all()
    assert vec[i] == 1 and vec.sum() == 1                       # only the shock candle fires, long (uptrend)
    assert signals_vectorized(d, dataclasses.replace(cfg, shock_vol=6.0))[i] == 0   # volume rule matters


def test_default_shock_books_are_4h_and_1h(monkeypatch):
    import config
    monkeypatch.setattr(config, "_load_env", lambda: None)
    monkeypatch.delenv("PAPER_SHOCK_TIMEFRAMES", raising=False)
    assert config.load_config().paper_shock_timeframes == ("4h", "1h")
