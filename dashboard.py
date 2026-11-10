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

TIMEFRAMES = ["30m", "1h", "4h"]
TESTED = {"4h": dict(ok=True, text="Backtest profitable · BTC 1.17 · ETH 1.23 · SOL 1.13 PF"),
          "1h": dict(ok=False, text="Backtest loses after fees · watch only"),
          "30m": dict(ok=False, text="Backtest loses after fees · watch only")}
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


def utc_epoch(text):
    try:
        return int(pd.Timestamp(str(text).replace(" UTC", ""), tz="UTC").timestamp())
    except (ValueError, TypeError):
        return None


def journal(cfg, tf):
    """Pair each paper 'entry' row with its 'stop'/'target' row -> one record per trade (full history, oldest first).
    The CSV logs the signal candle's open time; the bot enters when that candle CLOSES, so opened = time + period.
    An exit row's time is the candle in which the SL/TP was hit."""
    if not os.path.exists(cfg.trades_path):
        return []
    rows = pd.read_csv(cfg.trades_path).replace({np.nan: None}).to_dict("records")
    period, open_by_sym, out = RESOLUTION_SECONDS[tf], {}, []
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
        elif ev in ("stop", "target"):
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
    for tf in TIMEFRAMES:
        cfg = dataclasses.replace(CFG, resolution=tf, dry_run=True, state_file="")
        if not os.path.exists(cfg.state_path) and not os.path.exists(cfg.trades_path):
            continue
        s = RiskManager(cfg).state
        trades = read_trades(cfg.trades_path, f"Paper {tf}")
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
            positions.append(dict(source=f"Paper {tf}", symbol=sym, side=p["side"], size=p["size"], cval=p["cval"],
                                  entry=num(p["entry"]), sl=p["sl"], tp=p["tp"], mark=num(mark_price(sym)),
                                  opened=p.get("opened")))
        books.append(dict(tf=tf, equity=num(eq), start=CFG.bot_capital_usd, positions=positions, trades=trades,
                          journal=jr, wins=wins, losses=len(closed) - wins, closed=len(closed),
                          win_rate=num(100 * wins / len(closed), 1) if closed else None,
                          net=num(sum((x["pnl"] or 0) for x in closed)), avg_r=num(sum(rs) / len(rs), 2) if rs else None,
                          max_dd=num(100 * mdd, 1), last_checked=max(s.last_processed_by.values(), default=None),
                          halted=s.halted, halt_reason=s.halt_reason))
    return books


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
            out["error"] = str(e)[:300]
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
                paper_timeframes=list(CFG.paper_timeframes), capital=CFG.bot_capital_usd, risk=CFG.risk_per_trade, atr_mult=CFG.atr_stop_mult, rr=CFG.reward_risk,
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


RANGES = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "2Y": 730}


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
        if b["tf"] == tf:
            for p in b["positions"]:
                if p["symbol"] == sym:
                    lines.append(dict(label=f"Paper {tf}", entry=p["entry"], sl=p["sl"], tp=p["tp"]))
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
    print(f"Dashboard running: http://localhost:{a.port}   (Ctrl+C to stop; read-only, places no orders)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
