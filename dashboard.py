"""Local web dashboard: live prices, live forming candle, signals (30m / 1h / 4h), stop loss & target levels,
rule checklist, open trades and history.

  python dashboard.py              # then open http://localhost:8000
  python dashboard.py --port 8080

Read-only: it NEVER places or cancels orders. It binds to 127.0.0.1 (only your computer can open it).
On a server, view it through an SSH tunnel:  ssh -L 8000:localhost:8000 ubuntu@<SERVER_IP>

Endpoints: /api/overview (every 10 s), /api/live (every 2 s: tickers + forming candle), /api/chart (on demand).
"""
import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import argparse
import dataclasses
import json
import math
import re
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from config import RESOLUTION_SECONDS, load_config
from delta_client import DeltaClient
from risk import RiskManager
from strategy import add_indicators, levels, signal_at, signals_vectorized

TIMEFRAMES = ["30m", "1h", "4h", "1d"]
TESTED = {"4h": dict(ok=True, text="Backtest profitable · BTC 1.17 · ETH 1.23 · SOL 1.13 PF"),
          "1h": dict(ok=False, text="Backtest loses after fees · watch only"),
          "30m": dict(ok=False, text="Backtest loses after fees · watch only"),
          "1d": dict(ok=False, text="Fixed 1:3 weak · trailing stop profitable (paper test)")}
HERE = Path(__file__).resolve().parent

CFG = load_config()
CLIENT = DeltaClient(CFG.base_url, CFG.api_key, CFG.api_secret)
_cache, _lock = {}, threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now + ttl, val)
    return val


def num(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def tf_cfg(tf):
    return dataclasses.replace(CFG, resolution=tf)


def product(sym):
    def load():
        p = CLIENT.get_product(sym)
        return dict(pid=int(p["id"]), tick=float(p["tick_size"]), cval=float(p["contract_value"]))
    return cached(("product", sym), 3600, load)


def ticker(sym):
    def load():
        t = CLIENT.get_ticker(sym)
        q = t.get("quotes") or {}
        return dict(symbol=sym, mark=num(t.get("mark_price")), last=num(t.get("close")), spot=num(t.get("spot_price")),
                    change=num(t.get("mark_change_24h"), 3), high=num(t.get("high")), low=num(t.get("low")),
                    funding=num(t.get("funding_rate"), 4), bid=num(q.get("best_bid")), ask=num(q.get("best_ask")),
                    volume_usd=num(t.get("turnover_usd"), 0), oi_usd=num(t.get("oi_value_usd"), 0))
    return cached(("ticker", sym), 1.5, load)


def mark_price(sym):
    return ticker(sym)["mark"] or 0.0


def series(sym, tf, bars=500):
    """Closed candles with indicators (what the bot sees) + the same data including the still-forming candle."""
    def load():
        per = RESOLUTION_SECONDS[tf]
        now = int(time.time())
        rows = CLIENT.get_candles(sym, tf, now - (bars + 5) * per, now)
        raw = pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]].astype(float)
        closed = raw[raw["time"] + per <= now].reset_index(drop=True)
        return dict(closed=add_indicators(closed, tf_cfg(tf)), live=add_indicators(raw, tf_cfg(tf)))
    return cached(("series", sym, tf), 8, load)


def forming(sym, tf):
    def load():
        per = RESOLUTION_SECONDS[tf]
        now = int(time.time())
        rows = CLIENT.get_candles(sym, tf, now - 3 * per, now)
        rows = [r for r in rows if r["time"] + per > now]
        if not rows:
            return None
        r = rows[-1]
        return dict(time=int(r["time"]), open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                    close=float(r["close"]), closes_at=int(r["time"]) + per)
    return cached(("forming", sym, tf), 2, load)


def rules(r, prev, cfg, sig):
    """Plain-language checklist of the 3 rules for the latest CLOSED candle."""
    lo, hi = cfg.rsi_pullback, 100 - cfg.rsi_pullback
    rsi, prsi = float(r["rsi"]), float(prev["rsi"])
    up = r["ema_slow"] > r["ema_trend"] and r["close"] > r["ema_trend"]
    dn = r["ema_slow"] < r["ema_trend"] and r["close"] < r["ema_trend"]
    out = []
    if up:
        out.append(dict(name="Trend", state="pass", text="Uptrend: EMA 50 above EMA 200 and price above EMA 200. Only BUYS allowed."))
        dipped = prsi < lo or rsi < lo
        out.append(dict(name="Pullback", state="pass" if dipped else "wait",
                        text=f"RSI dipped below {lo:g}." if dipped else f"Waiting for RSI to dip below {lo:g} (now {rsi:.1f})."))
    elif dn:
        out.append(dict(name="Trend", state="pass", text="Downtrend: EMA 50 below EMA 200 and price below EMA 200. Only SELLS allowed."))
        dipped = prsi > hi or rsi > hi
        out.append(dict(name="Pullback", state="pass" if dipped else "wait",
                        text=f"RSI bounced above {hi:g}." if dipped else f"Waiting for RSI to bounce above {hi:g} (now {rsi:.1f})."))
    else:
        out.append(dict(name="Trend", state="fail", text="No clear trend (price and EMAs disagree). The bot stays out."))
        out.append(dict(name="Pullback", state="idle", text="Only checked when there is a clear trend."))
    if sig:
        out.append(dict(name="Trigger", state="pass", text=f"{sig.side.upper()} confirmed on candle close: {sig.reason}."))
    elif up and (prsi < lo or rsi < lo):
        out.append(dict(name="Trigger", state="wait", text=f"BUY fires when a candle closes with RSI back above {lo:g}."))
    elif dn and (prsi > hi or rsi > hi):
        out.append(dict(name="Trigger", state="wait", text=f"SELL fires when a candle closes with RSI back below {hi:g}."))
    else:
        out.append(dict(name="Trigger", state="idle", text="Needs trend + pullback first."))
    trend = "UP" if up else "DOWN" if dn else "MIXED"
    return trend, out


def plan(sym, cfg, side, atr, entry=None):
    p = product(sym)
    entry = entry or mark_price(sym)
    sl, tp = levels(side, entry, atr, cfg, p["tick"])
    size = RiskManager(cfg, path="state/dashboard_tmp.json").position_size(cfg.bot_capital_usd, entry, sl, p["cval"])
    return dict(side=side, entry=num(entry), sl=sl, tp=tp, size=size,
                risk=num(abs(entry - sl) * size * p["cval"]), reward=num(abs(tp - entry) * size * p["cval"]),
                sl_pct=num(abs(entry - sl) / entry * 100), tp_pct=num(abs(tp - entry) / entry * 100))


def card(sym, tf):
    cfg = tf_cfg(tf)
    s = series(sym, tf)
    d, lv = s["closed"], s["live"]
    r, prev = d.iloc[-1], d.iloc[-2]
    per = RESOLUTION_SECONDS[tf]
    sig = signal_at(d, len(d) - 1, cfg)
    trend, checklist = rules(r, prev, cfg, sig)
    out = dict(symbol=sym, tf=tf, tested=TESTED[tf], trading=(tf == CFG.resolution),
               last_closed=int(r["time"]), next_close=int(r["time"]) + 2 * per, period=per,
               close=num(r["close"]), rsi=num(r["rsi"], 1), atr=num(r["atr"]), trend=trend, rules=checklist,
               ema50=num(r["ema_slow"]), ema200=num(r["ema_trend"]), rsi_level=cfg.rsi_pullback,
               signal=None, preview=None, rsi_live=None)
    if sig:
        out["signal"] = dict(plan(sym, cfg, sig.side, sig.atr), reason=sig.reason)
    if len(lv) > len(d):                                     # forming candle exists: "if it closed now" preview
        out["rsi_live"] = num(lv["rsi"].iloc[-1], 1)
        pv = signal_at(lv, len(lv) - 1, cfg)
        if pv:
            out["preview"] = dict(side=pv.side, reason=pv.reason)
    return out


def read_trades(path, source):
    if not os.path.exists(path):
        return []
    t = pd.read_csv(path).tail(50).replace({np.nan: None})
    rows = t.to_dict("records")
    for row in rows:
        row["source"] = source
    return rows


def book_cfg(book):
    tf = book.split("-")[0]
    return dataclasses.replace(CFG, resolution=tf, dry_run=True, state_file="",
                               exit_mode="trail" if book.endswith("-trail") else "fixed")


def book_cfgs():
    """Paper records the bot keeps: the traded timeframe + PAPER_TIMEFRAMES (fixed SL + TP) and the trailing-stop
    variants (PAPER_TRAIL_TIMEFRAMES)."""
    fixed = [tf for tf in TIMEFRAMES if tf == CFG.resolution or tf in CFG.paper_timeframes]
    books = fixed + [f"{tf}-trail" for tf in TIMEFRAMES if tf in CFG.paper_trail_timeframes]
    return [(b, book_cfg(b)) for b in books]


def hist_books():
    """Strategy-history columns: every timeframe with the fixed exit (for comparison) + the trailing variants."""
    return list(TIMEFRAMES) + [f"{tf}-trail" for tf in TIMEFRAMES if tf in CFG.paper_trail_timeframes]


def book_label(book):
    return book.replace("-trail", " trail")


def utc_epoch(text):
    try:
        return int(pd.Timestamp(str(text).replace(" UTC", ""), tz="UTC").timestamp())
    except (ValueError, TypeError):
        return None


def journal(cfg, book):
    """Pair each paper 'entry' row with its 'stop'/'target' row -> one record per trade (full history, oldest first).
    The CSV logs the signal candle's open time; the bot enters when that candle CLOSES, so opened = time + period.
    An exit row's time is the candle in which the SL/TP was hit."""
    if not os.path.exists(cfg.trades_path):
        return []
    rows = pd.read_csv(cfg.trades_path).replace({np.nan: None}).to_dict("records")
    period, open_by_sym, out, tf = cfg.period_seconds, {}, [], book
    for r in rows:
        sym, ev = r.get("symbol"), r.get("event")
        if ev == "entry":
            t0 = utc_epoch(r.get("time"))
            rec = dict(tf=tf, symbol=sym, side=r.get("side"), size=num(r.get("size"), 0), entry=num(r.get("entry"), 6),
                       sl=num(r.get("sl"), 6), tp=num(r.get("tp"), 6), opened=t0 + period if t0 else None,
                       reason=r.get("reason"), status="open", exit=None, closed=None, pnl=None, r=None,
                       equity_before=num(r.get("equity")))
            open_by_sym[sym] = rec
            out.append(rec)
        elif ev in ("stop", "target", "trail"):
            rec = open_by_sym.pop(sym, None)
            if rec is None:                                   # exit without a logged entry: keep it visible anyway
                rec = dict(tf=tf, symbol=sym, side=r.get("side"), size=num(r.get("size"), 0), entry=num(r.get("entry"), 6),
                           sl=num(r.get("sl"), 6), tp=num(r.get("tp"), 6), opened=None, reason=None)
                out.append(rec)
            t1 = utc_epoch(r.get("time"))
            rec.update(status="won" if (r.get("pnl") or 0) > 0 else "lost", hit=ev, exit=num(r.get("exit"), 6),
                       closed=t1 + period if t1 else None, pnl=num(r.get("pnl"), 4), equity_after=num(r.get("equity")))
            try:
                risk_usd = abs(rec["entry"] - rec["sl"]) * rec["size"] * product(sym)["cval"]
                rec["r"] = num(rec["pnl"] / risk_usd, 2) if risk_usd else None
            except Exception:
                rec["r"] = None
    return out


def paper_books():
    books = []
    for tf, cfg in book_cfgs():
        if not os.path.exists(cfg.state_path) and not os.path.exists(cfg.trades_path):
            continue
        s = RiskManager(cfg).state
        trades = read_trades(cfg.trades_path, f"Paper {book_label(tf)}")
        jr = journal(cfg, tf)
        closed = [x for x in jr if x["status"] != "open"]
        wins = sum(1 for x in closed if x["status"] == "won")
        eq = s.paper_equity or CFG.bot_capital_usd
        peak, mdd, run = CFG.bot_capital_usd, 0.0, CFG.bot_capital_usd   # max drawdown from the closed-trade curve
        for x in closed:
            run += x["pnl"] or 0
            peak = max(peak, run)
            mdd = max(mdd, (peak - run) / peak if peak else 0)
        rs = [x["r"] for x in closed if x.get("r") is not None]
        positions = []
        for sym, p in s.paper_positions.items():
            positions.append(dict(source=f"Paper {book_label(tf)}", symbol=sym, side=p["side"], size=p["size"],
                                  cval=p["cval"], entry=num(p["entry"]), sl=num(p["sl"], 6), tp=p["tp"],
                                  sl0=num(p.get("sl0"), 6), trail=p.get("mode") == "trail", mark=num(mark_price(sym)),
                                  opened=p.get("opened")))
        books.append(dict(tf=tf, res=cfg.resolution, mode=cfg.exit_mode, label=book_label(tf), equity=num(eq), start=CFG.bot_capital_usd, positions=positions, trades=trades,
                          journal=jr, wins=wins, losses=len(closed) - wins, closed=len(closed),
                          win_rate=num(100 * wins / len(closed), 1) if closed else None,
                          net=num(sum((x["pnl"] or 0) for x in closed)), avg_r=num(sum(rs) / len(rs), 2) if rs else None,
                          max_dd=num(100 * mdd, 1), last_checked=max(s.last_processed_by.values(), default=None),
                          halted=s.halted, halt_reason=s.halt_reason))
    return books


def friendly_error(msg):
    """Turn Delta's IP-whitelist error into a clear instruction (the raw error is still logged by the bot)."""
    m = re.search(r'ip_not_whitelisted_for_api_key.*?"client_ip": "([^"]+)"', msg)
    if m:
        ip = m.group(1)
        kind = "IPv6" if ":" in ip else "IPv4"
        return (f"Your current {kind} address {ip} is not whitelisted on your Delta API key. "
                f"Add it in Delta → Account → API Keys → edit key → whitelisted IPs (mobile-data IPs change often; "
                f"a server with a fixed IP fixes this for good).")
    return msg[:300]


def live_account():
    if not (CFG.api_key and CFG.api_secret):
        return dict(enabled=False)

    def load():
        out = dict(enabled=True, positions=[], orders=[], error=None, equity=None)
        try:
            out["equity"] = num(CLIENT.get_wallet_equity(CFG.wallet_asset), 4)
            for sym in CFG.symbols:
                pid = product(sym)["pid"]
                pos = CLIENT.get_position(pid)
                size = float(pos.get("size", 0) or 0)
                sl = tp = None
                for o in CLIENT.get_open_orders(pid):
                    kind = str(o.get("stop_order_type") or o.get("order_type") or "")
                    price = num(o.get("stop_price") or o.get("limit_price"))
                    out["orders"].append(dict(symbol=sym, side=o.get("side"), size=o.get("size"), type=kind,
                                              price=price, state=o.get("state")))
                    if "stop_loss" in kind:
                        sl = price
                    elif "take_profit" in kind:
                        tp = price
                if size:
                    out["positions"].append(dict(source="Delta account", symbol=sym, side="buy" if size > 0 else "sell",
                                                 size=abs(size), cval=product(sym)["cval"],
                                                 entry=num(pos.get("entry_price")), sl=sl, tp=tp, mark=num(mark_price(sym))))
        except Exception as e:                       # show the error on the page instead of crashing
            out["error"] = friendly_error(str(e))
        return out
    return cached(("live",), 10, load)


def overview():
    cards, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:            # warm caches in parallel (first load ~0.5 s)
        jobs = [ex.submit(series, s, tf) for s in CFG.symbols for tf in TIMEFRAMES]
        jobs += [ex.submit(ticker, s) for s in CFG.symbols] + [ex.submit(live_account)]
        for j in jobs:
            try:
                j.result()
            except Exception:
                pass                                         # reported per card below
    for sym in CFG.symbols:
        for tf in TIMEFRAMES:
            try:
                cards.append(card(sym, tf))
            except Exception as e:
                errors.append(f"{sym} {tf}: {e}")
    return dict(generated=int(time.time()), mode="PAPER" if CFG.dry_run else "LIVE", env=CFG.env,
                strategy=CFG.strategy, resolution=CFG.resolution, symbols=list(CFG.symbols),
                paper_timeframes=list(CFG.paper_timeframes), books=[b for b, _ in book_cfgs()], hist_books=hist_books(),
                trail=dict(activate_r=CFG.trail_activate_r, atr=CFG.trail_atr), capital=CFG.bot_capital_usd, risk=CFG.risk_per_trade, atr_mult=CFG.atr_stop_mult, rr=CFG.reward_risk,
                rsi_level=CFG.rsi_pullback, max_trades=CFG.max_trades_per_day, daily_loss=CFG.daily_loss_limit,
                cards=cards, paper=paper_books(), live=live_account(),
                live_trades=read_trades(f"{CFG.log_dir}/trades.csv", "LIVE"), errors=errors)


def live(sym, tf):
    out = dict(server_time=time.time(), tickers={}, forming=None, errors=[])
    for s in CFG.symbols:
        try:
            out["tickers"][s] = ticker(s)
        except Exception as e:
            out["errors"].append(f"{s}: {e}")
    if sym in CFG.symbols and tf in TIMEFRAMES:
        try:
            out["forming"] = forming(sym, tf)
        except Exception as e:
            out["errors"].append(f"candle: {e}")
    return out


RANGES = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "2Y": 730, "ALL": 1100}


def history(sym, tf, days):
    """Older candles, fetched in parallel 1000-candle chunks and cached for 10 min (history does not change)."""
    def load():
        per = RESOLUTION_SECONDS[tf]
        end = int(time.time())
        start = end - days * 86400 - (CFG.ema_trend + 50) * per        # extra warm-up so EMA200 is right at the left edge
        chunks = [(a, min(a + 1000 * per, end)) for a in range(start, end, 1000 * per)]
        with ThreadPoolExecutor(max_workers=6) as ex:
            parts = list(ex.map(lambda c: CLIENT.get_candles(sym, tf, c[0], c[1]), chunks))
        rows = [r for part in parts for r in part]
        return pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]].astype(float)
    return cached(("hist", sym, tf, days), 600, load)


def chart(sym, tf, rng="3M"):
    cfg = tf_cfg(tf)
    per = RESOLUTION_SECONDS[tf]
    days = RANGES.get(rng, 91)
    recent = series(sym, tf)["live"][["time", "open", "high", "low", "close", "volume"]]   # fresh tail (8 s cache)
    raw = pd.concat([history(sym, tf, days), recent]).drop_duplicates("time", keep="last").sort_values("time")
    d = add_indicators(raw.reset_index(drop=True), cfg)
    now = time.time()
    closed_mask = (d["time"] + per <= now).to_numpy()
    sig = signals_vectorized(d, cfg)
    sig[~closed_mask] = 0                                           # never mark the still-forming candle
    first = now - days * 86400
    view = d[d["time"] >= first]
    sig = sig[d["time"].to_numpy() >= first]
    markers = [dict(time=int(t), side="buy" if v > 0 else "sell") for t, v in zip(view["time"], sig) if v]
    lines = []
    for b in paper_books():
        if b["res"] == tf:
            for p in b["positions"]:
                if p["symbol"] == sym:
                    lines.append(dict(label=f"Paper {b['label']}", entry=p["entry"], sl=p["sl"], tp=p["tp"]))
    for p in live_account().get("positions", []):
        if p["symbol"] == sym:
            lines.append(dict(label="Delta", entry=p["entry"], sl=p["sl"], tp=p["tp"]))
    c = card(sym, tf)
    if c["signal"]:
        lines.append(dict(label="Signal", entry=c["signal"]["entry"], sl=c["signal"]["sl"], tp=c["signal"]["tp"]))
    col = lambda k, nd=4: [num(v, nd) for v in view[k]]
    return dict(symbol=sym, tf=tf, range=rng, markers=markers, lines=lines, rsi_level=CFG.rsi_pullback,
                n=len(view), first=int(view["time"].iloc[0]) if len(view) else None,
                cols=dict(t=[int(t) for t in view["time"]], o=col("open"), h=col("high"), l=col("low"), c=col("close"),
                          e50=col("ema_slow"), e200=col("ema_trend"), rsi=col("rsi", 2)))


# ---------------------------------------------------------------- strategy history (every past trade, all timeframes)
HIST_DAYS = 1100                      # Delta keeps ~2.5 years of candles; ask for a bit more
SOURCES = ("delta", "binance")
_hist = {src: dict(state="idle", done=0, total=0, books={}, updated=None, error=None, note=None) for src in SOURCES}
_hist_lock = threading.Lock()

# ------------------------------------------------------------- Binance long history (research only, never for trading)
BINANCE = "https://fapi.binance.com/fapi/v1/klines"     # free public API: USDT-margined perpetuals, no account/key
BINANCE_START = 1567296000                               # 2019-09-01 (first Binance perpetuals)
BINANCE_DIR = HERE / "data" / "binance"                  # cached on disk (git-ignored); later runs fetch only new candles
_bn_lock = threading.Lock()


def binance_symbol(sym):
    return sym[:-3] + "USDT" if sym.endswith("USD") else sym


def binance_candles(sym, tf, progress=None):
    """Closed + forming candles from Binance since 2019, cached in data/binance/<SYM>_<tf>.pkl and topped up."""
    import requests
    per = RESOLUTION_SECONDS[tf]
    path = BINANCE_DIR / f"{binance_symbol(sym)}_{tf}.pkl"
    with _bn_lock:
        cached_df = pd.read_pickle(path) if path.exists() else None
        start = int(cached_df["time"].iloc[-2]) if cached_df is not None and len(cached_df) > 2 else BINANCE_START
        rows, cur = [], start * 1000
        while True:
            for attempt in range(4):
                try:
                    r = requests.get(BINANCE, params=dict(symbol=binance_symbol(sym), interval=tf, startTime=cur, limit=1500),
                                     timeout=20)
                    if r.status_code in (418, 429):          # rate limited: back off
                        time.sleep(30)
                        continue
                    r.raise_for_status()
                    part = r.json()
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(3)
            if not part:
                break
            rows += part
            cur = part[-1][0] + per * 1000
            if progress:
                progress(len(rows))
            if len(part) < 1500:
                break
            time.sleep(0.35)                                # ~170 requests/min, far below Binance's limit
        new = pd.DataFrame([r[:6] for r in rows], columns=["time", "open", "high", "low", "close", "volume"]).astype(float)
        new["time"] = (new["time"] // 1000)
        df = new if cached_df is None else pd.concat([cached_df, new])
        df = df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
        BINANCE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_pickle(path)
        return df


def _hist_book(sym, book, src="delta"):
    from backtest import simulate_trades
    tf = book.split("-")[0]
    cfg = dataclasses.replace(tf_cfg(tf), exit_mode="trail" if book.endswith("-trail") else "fixed")
    if src == "binance":
        raw = binance_candles(sym, tf)
    else:
        recent = series(sym, tf)["live"][["time", "open", "high", "low", "close", "volume"]]
        raw = pd.concat([history(sym, tf, HIST_DAYS), recent]).drop_duplicates("time", keep="last").sort_values("time")
    d = add_indicators(raw.reset_index(drop=True), cfg)
    cap = CFG.bot_capital_usd
    trades = simulate_trades(d, cfg, capital=cap, now=time.time())
    cval = product(sym)["cval"]
    rows = []
    for x in trades:
        rows.append(dict(side=x["side"], sig=x["signal_time"], t0=x["entry_time"], entry=num(x["entry"], 6),
                         sl=num(x["sl0"], 6), slx=num(x["sl"], 6), tp=num(x["tp"], 6), t1=x["exit_time"], exit=num(x["exit"], 6),
                         res=x["result"], pnl=num(x["pnl"]), r=num(x["r"]), eq=num(x["equity"]),
                         ct=num(x["qty"] / cval, 1), risk=num(x["risk"])))
    closed = [x for x in trades if x["result"] != "open"]
    wins = [x for x in closed if x["pnl"] > 0]
    gp, gl = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in closed if x["pnl"] <= 0)
    years = {}
    for x in closed:
        y = time.gmtime(x["exit_time"]).tm_year
        b = years.setdefault(y, dict(n=0, w=0, pnl=0.0))
        b["n"] += 1; b["w"] += int(x["pnl"] > 0); b["pnl"] += float(x["pnl"])
    end_eq = closed[-1]["equity"] if closed else cap
    return dict(symbol=sym, tf=book, res=tf, mode=cfg.exit_mode, label=book_label(book), src=src,
                first=int(d["time"].iloc[0]), last=int(d["time"].iloc[-1]), candles=len(d),
                trades=rows, n=len(closed), wins=len(wins), win_rate=num(100 * len(wins) / len(closed), 1) if closed else None,
                pf=num(gp / gl) if gl else None, net=num(end_eq - cap), ret=num(100 * (end_eq / cap - 1), 1),
                max_dd=num(100 * max([x["dd"] for x in closed] or [0]), 1),
                avg_r=num(sum(x["r"] for x in closed) / len(closed)) if closed else None,
                start=cap, end=num(end_eq), open=any(x["result"] == "open" for x in trades),
                years={str(k): dict(n=v["n"], w=v["w"], pnl=num(v["pnl"])) for k, v in sorted(years.items())})


def _hist_run(src):
    jobs = [(s, b) for s in CFG.symbols for b in hist_books()]
    h = _hist[src]
    with _hist_lock:
        h.update(state="running", done=0, total=len(jobs), error=None)
    for s, tf in jobs:                                         # one at a time: be gentle with the APIs
        try:
            with _hist_lock:
                h["note"] = f"{'downloading/updating' if src == 'binance' else 'replaying'} {s} {tf}"
            book = _hist_book(s, tf, src)
            with _hist_lock:
                h["books"][f"{s}|{tf}"] = book
        except Exception as e:
            with _hist_lock:
                h["error"] = f"{s} {tf}: {str(e)[:200]}"
        with _hist_lock:
            h["done"] += 1
    with _hist_lock:
        h.update(state="ready", updated=int(time.time()), note=None)


def strategy_history(src="delta"):
    """Start (or refresh every 30 min) the background run for a data source; return whatever is ready so far."""
    src = src if src in SOURCES else "delta"
    h = _hist[src]
    with _hist_lock:
        stale = h["state"] == "idle" or (h["state"] == "ready" and time.time() - (h["updated"] or 0) > 1800)
        if stale:
            h["state"] = "running"
    if stale:
        threading.Thread(target=_hist_run, args=(src,), daemon=True).start()
    with _hist_lock:
        return dict(src=src, state=h["state"], done=h["done"], total=h["total"] or len(CFG.symbols) * len(hist_books()),
                    updated=h["updated"], error=h["error"], note=h["note"], capital=CFG.bot_capital_usd,
                    risk=CFG.risk_per_trade, fee=CFG.fee_rate, slippage_bps=CFG.slippage_bps,
                    books=list(h["books"].values()))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        sym = q.get("symbol", CFG.symbols[0]).upper()
        tf = q.get("tf", CFG.resolution)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/overview":
                return self._send(200, json.dumps(overview()), "application/json")
            if u.path == "/api/history":
                return self._send(200, json.dumps(strategy_history(q.get("src", "delta"))), "application/json")
            if u.path == "/api/live":
                return self._send(200, json.dumps(live(sym, tf)), "application/json")
            if u.path == "/api/chart":
                if sym not in CFG.symbols or tf not in TIMEFRAMES:
                    return self._send(400, '{"error":"bad symbol/tf"}', "application/json")
                return self._send(200, json.dumps(chart(sym, tf, q.get("range", "3M"))), "application/json")
            self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps(dict(error=str(e)[:300])), "application/json")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you know what you are doing")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    threading.Thread(target=strategy_history, daemon=True).start()     # start computing past trades right away
    print(f"Dashboard running: http://localhost:{a.port}   (Ctrl+C to stop; read-only, places no orders)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
