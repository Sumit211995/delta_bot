"""Local web dashboard: live signals (30m / 1h / 4h), stop loss & target levels, chart, open trades, history.

  python dashboard.py              # then open http://localhost:8000
  python dashboard.py --port 8080

Read-only: it NEVER places or cancels orders. It binds to 127.0.0.1 (only your computer can open it).
On a server, view it through an SSH tunnel:  ssh -L 8000:localhost:8000 ubuntu@<SERVER_IP>
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from config import RESOLUTION_SECONDS, load_config
from delta_client import DeltaClient
from risk import RiskManager
from strategy import add_indicators, levels, signal_at

TIMEFRAMES = ["30m", "1h", "4h"]
TESTED = {"4h": "Backtest: profitable (BTC PF 1.17, ETH PF 1.23)",
          "1h": "Backtest: loses after fees (watch only)",
          "30m": "Backtest: loses after fees (watch only)"}
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


def product(sym):
    def load():
        p = CLIENT.get_product(sym)
        return dict(pid=int(p["id"]), tick=float(p["tick_size"]), cval=float(p["contract_value"]))
    return cached(("product", sym), 3600, load)


def mark_price(sym):
    return cached(("mark", sym), 10, lambda: float(CLIENT.get_ticker(sym).get("mark_price") or 0))


def candles(sym, tf, bars=500):
    def load():
        per = RESOLUTION_SECONDS[tf]
        now = int(time.time())
        rows = CLIENT.get_candles(sym, tf, now - (bars + 5) * per, now)
        df = pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]].astype(float)
        df = df[df["time"] + per <= now].reset_index(drop=True)   # closed candles only (same as the bot)
        return add_indicators(df, dataclasses.replace(CFG, resolution=tf))
    return cached(("candles", sym, tf), 30, load)


def trend_and_setup(r, cfg):
    lo, hi = cfg.rsi_pullback, 100 - cfg.rsi_pullback
    rsi = float(r["rsi"])
    if r["ema_slow"] > r["ema_trend"] and r["close"] > r["ema_trend"]:
        if rsi < lo:
            return "UP", f"Uptrend + pullback: RSI {rsi:.0f} is below {lo:g}. BUY fires if a candle closes with RSI back above {lo:g}."
        return "UP", f"Uptrend. Waiting for a dip: RSI {rsi:.0f} must fall below {lo:g}, then close back above it."
    if r["ema_slow"] < r["ema_trend"] and r["close"] < r["ema_trend"]:
        if rsi > hi:
            return "DOWN", f"Downtrend + bounce: RSI {rsi:.0f} is above {hi:g}. SELL fires if a candle closes with RSI back below {hi:g}."
        return "DOWN", f"Downtrend. Waiting for a bounce: RSI {rsi:.0f} must rise above {hi:g}, then close back below it."
    return "MIXED", "No clear trend (price and EMAs disagree). The bot stays out."


def card(sym, tf):
    cfg = dataclasses.replace(CFG, resolution=tf)
    d = candles(sym, tf)
    r = d.iloc[-1]
    per = RESOLUTION_SECONDS[tf]
    trend, setup = trend_and_setup(r, cfg)
    out = dict(symbol=sym, tf=tf, tested=TESTED.get(tf, ""), trading=(tf == CFG.resolution),
               last_closed=int(r["time"]), next_close=int(r["time"]) + 2 * per,
               close=num(r["close"]), rsi=num(r["rsi"], 1), atr=num(r["atr"]), trend=trend, setup=setup,
               ema50=num(r["ema_slow"]), ema200=num(r["ema_trend"]), signal=None)
    sig = signal_at(d, len(d) - 1, cfg)
    if sig:
        p = product(sym)
        entry = mark_price(sym) or float(r["close"])
        sl, tp = levels(sig.side, entry, sig.atr, cfg, p["tick"])
        size = RiskManager(cfg, path="state/dashboard_tmp.json").position_size(cfg.bot_capital_usd, entry, sl, p["cval"])
        out["signal"] = dict(side=sig.side, reason=sig.reason, entry=num(entry), sl=sl, tp=tp, size=size,
                             risk=num(abs(entry - sl) * size * p["cval"]), reward=num(abs(tp - entry) * size * p["cval"]))
    return out


def read_trades(path, source):
    if not os.path.exists(path):
        return []
    t = pd.read_csv(path).tail(50)
    t = t.replace({np.nan: None})
    rows = t.to_dict("records")
    for row in rows:
        row["source"] = source
    return rows


def paper_books():
    books = []
    for tf in TIMEFRAMES:
        cfg = dataclasses.replace(CFG, resolution=tf, dry_run=True, state_file="")
        if not os.path.exists(cfg.state_path) and not os.path.exists(cfg.trades_path):
            continue
        s = RiskManager(cfg).state
        trades = read_trades(cfg.trades_path, f"paper {tf}")
        closed = [x for x in trades if x.get("event") in ("stop", "target")]
        positions = []
        for sym, p in s.paper_positions.items():
            m = mark_price(sym)
            direction = 1 if p["side"] == "buy" else -1
            positions.append(dict(source=f"paper {tf}", symbol=sym, side=p["side"], size=p["size"],
                                  entry=num(p["entry"]), sl=p["sl"], tp=p["tp"], mark=num(m), opened=p.get("opened"),
                                  upnl=num((m - p["entry"]) * direction * p["size"] * p["cval"]) if m else None))
        books.append(dict(tf=tf, equity=num(s.paper_equity or CFG.bot_capital_usd), start=CFG.bot_capital_usd,
                          positions=positions, trades=trades, wins=sum(1 for x in closed if (x.get("pnl") or 0) > 0),
                          losses=sum(1 for x in closed if (x.get("pnl") or 0) <= 0),
                          net=num(sum((x.get("pnl") or 0) for x in closed)), halted=s.halted, halt_reason=s.halt_reason))
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
                orders = CLIENT.get_open_orders(pid)
                sl = tp = None
                for o in orders:
                    kind = str(o.get("stop_order_type") or o.get("order_type") or "")
                    price = num(o.get("stop_price") or o.get("limit_price"))
                    out["orders"].append(dict(symbol=sym, side=o.get("side"), size=o.get("size"), type=kind,
                                              price=price, state=o.get("state")))
                    if "stop_loss" in kind:
                        sl = price
                    elif "take_profit" in kind:
                        tp = price
                if size:
                    entry = float(pos.get("entry_price") or 0)
                    m = mark_price(sym)
                    out["positions"].append(dict(source="Delta account", symbol=sym, side="buy" if size > 0 else "sell",
                                                 size=abs(size), entry=num(entry), sl=sl, tp=tp, mark=num(m),
                                                 upnl=num((m - entry) * size * product(sym)["cval"]) if m else None))
        except Exception as e:                       # show the error on the page instead of crashing
            out["error"] = str(e)[:300]
        return out
    return cached(("live",), 15, load)


def overview():
    cards, errors = [], []
    for sym in CFG.symbols:
        for tf in TIMEFRAMES:
            try:
                cards.append(card(sym, tf))
            except Exception as e:
                errors.append(f"{sym} {tf}: {e}")
    live_trades = read_trades(f"{CFG.log_dir}/trades.csv", "LIVE")
    return dict(generated=int(time.time()), mode="PAPER" if CFG.dry_run else "LIVE", env=CFG.env,
                strategy=CFG.strategy, resolution=CFG.resolution, symbols=list(CFG.symbols),
                capital=CFG.bot_capital_usd, risk=CFG.risk_per_trade, atr_mult=CFG.atr_stop_mult, rr=CFG.reward_risk,
                rsi_level=CFG.rsi_pullback, cards=cards, paper=paper_books(), live=live_account(),
                live_trades=live_trades, errors=errors)


def chart(sym, tf):
    cfg = dataclasses.replace(CFG, resolution=tf)
    d = candles(sym, tf).tail(300).reset_index(drop=True)
    full = candles(sym, tf)
    off = len(full) - len(d)
    markers = []
    for i in range(len(d)):
        s = signal_at(full, off + i, cfg)
        if s:
            markers.append(dict(time=int(d["time"].iloc[i]), side=s.side))
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
    rec = lambda col: [dict(time=int(t), value=num(v, 4)) for t, v in zip(d["time"], d[col]) if num(v) is not None]
    return dict(symbol=sym, tf=tf, markers=markers, lines=lines, rsi_level=CFG.rsi_pullback,
                candles=[dict(time=int(r.time), open=r.open, high=r.high, low=r.low, close=r.close)
                         for r in d.itertuples()],
                ema50=rec("ema_slow"), ema200=rec("ema_trend"), rsi=rec("rsi"))


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
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/overview":
                return self._send(200, json.dumps(overview()), "application/json")
            if u.path == "/api/chart":
                sym = q.get("symbol", CFG.symbols[0]).upper()
                tf = q.get("tf", CFG.resolution)
                if sym not in CFG.symbols or tf not in TIMEFRAMES:
                    return self._send(400, '{"error":"bad symbol/tf"}', "application/json")
                return self._send(200, json.dumps(chart(sym, tf)), "application/json")
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
