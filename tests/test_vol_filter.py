"""High-volatility filter (VOL_FILTER_BOOKS): signal_at and signals_vectorized must agree, and only the listed
books are filtered."""
import dataclasses

import numpy as np

from config import Config
from strategy import add_indicators, signal_at, signals_vectorized
from tests.test_core import synthetic


def _cfg(book_trail=True, **kw):
    return dataclasses.replace(Config(), resolution="1d", exit_mode="trail" if book_trail else "fixed",
                               vol_filter_books=("1d-trail",), vol_filter_n=100, **kw)


def test_filter_only_on_listed_book():
    df = synthetic(1500, seed=5)
    on, off = _cfg(True), _cfg(False)
    v_on = signals_vectorized(add_indicators(df, on), on)
    v_off = signals_vectorized(add_indicators(df, off), off)
    assert "atr_rank" not in add_indicators(df, off)
    assert np.abs(v_on).sum() < np.abs(v_off).sum()                      # some signals removed
    assert np.all((v_on == 0) | (v_on == v_off))                          # never adds or flips a signal


def test_vectorized_matches_signal_at_with_filter():
    cfg = _cfg(True, vol_filter_max=0.6)
    d = add_indicators(synthetic(1500, seed=9), cfg)
    v = signals_vectorized(d, cfg)
    for i in range(len(d)):
        s = signal_at(d, i, cfg)
        assert v[i] == (0 if s is None else (1 if s.side == "buy" else -1)), i
        if v[i]:
            assert not d["atr_rank"].iloc[i] > cfg.vol_filter_max
