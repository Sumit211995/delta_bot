"""Live trading loop for Delta Exchange India.

  python bot.py --check     # connectivity, product info, balance, position, hypothetical signal. No orders.
  python bot.py             # run (DRY_RUN=1: paper trades with simulated SL/TP; DRY_RUN=0: real orders)
  python bot.py --once      # process the latest closed candle once and exit (handy for testing)
  python bot.py --status    # show risk state, paper equity and open paper positions
  python bot.py --panic     # cancel all orders and close positions on every symbol now
"""
import argparse
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

import notifier
import paper
from config import load_config, validate
from delta_client import DeltaClient, DeltaError
from risk import RiskManager
from strategy import add_indicators, levels, ml_pass, signal_at

LOG_COLUMNS = ["time", "symbol", "event", "side", "size", "entry", "sl", "tp", "exit", "pnl", "equity",
               "live", "order_id", "reason"]


def setup_logging(cfg):
    os.makedirs(cfg.log_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(f"{cfg.log_dir}/bot.log"), logging.StreamHandler()])


def fmt_t(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fetch_closed_candles(client, cfg, symbol, bars=400) -> pd.DataFrame:
    now = int(time.time())
    rows = client.get_candles(symbol, cfg.resolution, now - (bars + 5) * cfg.period_seconds, now)
    df = pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]].astype(float)
    df = df[df["time"] + cfg.period_seconds <= now]            # drop the still-forming candle
    return df.reset_index(drop=True)


def log_trade(cfg, row: dict):
    os.makedirs(cfg.log_dir, exist_ok=True)
    path = f"{cfg.log_dir}/{'paper_trades' if cfg.dry_run else 'trades'}.csv"
    pd.DataFrame([{k: row.get(k, "") for k in LOG_COLUMNS}]).to_csv(
        path, mode="a", header=not os.path.exists(path), index=False)


def protection_ok(client, pid) -> bool:
    """After an entry, make sure a protective order exists. If not, the caller flattens immediately."""
    for _ in range(5):
        time.sleep(1.5)
        try:
            if client.position_size(pid) == 0:
                return True                                    # already closed (stop/target hit)
            if len(client.get_open_orders(pid)) >= 1:
                return True
        except DeltaError as e:
            logging.warning("protection check error: %s", e)
    return False


def load_markets(client, cfg):
    markets = []
    for sym in cfg.symbols:
        p = client.get_product(sym)
        markets.append(dict(symbol=sym, pid=int(p["id"]), tick=float(p["tick_size"]),
                            cval=float(p["contract_value"])))
    return markets


def panic(client, cfg, markets):
    for m in markets:
        client.cancel_all(m["pid"])
        res = client.close_position(m["pid"])
        notifier.send(cfg, f"PANIC executed on {m['symbol']}: {res}")


def settle_paper(cfg, risk, m, df):
    pos = risk.state.paper_positions.get(m["symbol"])
    if not pos:
        return
    hit = paper.settle(pos, df)
    if hit is None:
        risk.save()
        return
    exit_p, t, why = hit
    pl = paper.pnl(pos, exit_p, cfg.fee_rate)
    risk.state.paper_equity += pl
    del risk.state.paper_positions[m["symbol"]]
    risk.save()
    log_trade(cfg, dict(time=fmt_t(t), symbol=m["symbol"], event=why, side=pos["side"], size=pos["size"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"], exit=exit_p, pnl=round(pl, 4),
                        equity=round(risk.state.paper_equity, 2), live=False))
    notifier.send(cfg, f"PAPER {m['symbol']} {pos['side'].upper()} closed at {why.upper()} {exit_p} "
                       f"P&L {pl:+.2f} | paper equity {risk.state.paper_equity:.2f}")


def step(client, cfg, risk, m, live_sizes):
    sym, pid = m["symbol"], m["pid"]
    live = not cfg.dry_run

    if live:                                                    # notify when SL/TP closed a live position
        size_now = client.position_size(pid)
        if live_sizes.get(sym) and size_now == 0:
            notifier.send(cfg, f"LIVE {sym} position closed (stop or target hit). Check P&L in the Delta app.")
        live_sizes[sym] = size_now

    df = fetch_closed_candles(client, cfg, sym)
    last_t = int(df["time"].iloc[-1])
    if last_t <= risk.state.last_processed_by.get(sym, 0):
        return
    risk.state.last_processed_by[sym] = last_t                  # mark first: never double-trade a candle
    risk.save()
    if not live:
        settle_paper(cfg, risk, m, df)

    d = add_indicators(df, cfg)
    sig = signal_at(d, len(d) - 1, cfg)
    ts = fmt_t(last_t)
    if sig is None:
        logging.info("%s candle %s closed: no signal (close=%s rsi=%.1f)", sym, ts,
                     df["close"].iloc[-1], d["rsi"].iloc[-1])
        return
    if not ml_pass(sig, cfg):
        logging.info("%s candle %s: %s signal rejected by ML filter", sym, ts, sig.side)
        return

    if live and live_sizes.get(sym):
        logging.info("%s signal ignored: position already open", sym)
        return
    if not live and sym in risk.state.paper_positions:
        logging.info("%s signal ignored: paper position already open", sym)
        return

    if live:
        equity = client.get_wallet_equity(cfg.wallet_asset)
        if equity is None:
            notifier.send(cfg, f"Wallet asset {cfg.wallet_asset} not found; no trade (fail-safe).")
            return
    else:
        if risk.state.paper_equity <= 0:
            risk.state.paper_equity = cfg.bot_capital_usd
        equity = risk.state.paper_equity
    ok, why = risk.check(equity)
    if not ok:
        notifier.send(cfg, f"{sym} signal {sig.side} skipped: {why}")
        return

    entry = float(client.get_ticker(sym).get("mark_price") or df["close"].iloc[-1])
    sl, tp = levels(sig.side, entry, sig.atr, cfg, m["tick"])
    size = risk.position_size(equity, entry, sl, m["cval"])
    if size < 1:
        notifier.send(cfg, f"{sym} signal {sig.side} skipped: size < 1 contract (capital too small for this stop).")
        return

    risk_amt = abs(entry - sl) * size * m["cval"]
    msg = (f"{'LIVE' if live else 'PAPER'} {sig.side.upper()} {size} {sym} @~{entry:.2f} "
           f"SL {sl} TP {tp} (risk ~{risk_amt:.2f}) | {sig.reason}")
    notifier.send(cfg, msg)
    row = dict(time=ts, symbol=sym, event="entry", side=sig.side, size=size, entry=entry, sl=sl, tp=tp,
               equity=round(equity, 2), live=live, reason=sig.reason)

    if not live:
        risk.state.paper_positions[sym] = dict(side=sig.side, size=size, entry=entry, sl=sl, tp=tp,
                                               cval=m["cval"], checked_until=last_t, opened=ts)
        risk.record_trade()
        log_trade(cfg, row)
        return

    try:
        res = client.place_bracket_market(pid, size, sig.side, sl, tp, uuid.uuid4().hex[:24])
    except DeltaError as e:
        notifier.send(cfg, f"{sym} ORDER FAILED: {e}")
        return
    risk.record_trade()
    log_trade(cfg, {**row, "order_id": res.get("result", {}).get("id")})
    if not protection_ok(client, pid):
        client.cancel_all(pid)
        client.close_position(pid)
        risk.halt(f"{sym}: no protective order detected after entry")
        notifier.send(cfg, f"{sym} position flattened and bot HALTED: protective order not confirmed.")
    live_sizes[sym] = client.position_size(pid)


def check(client, cfg, markets):
    print(f"env={cfg.env} dry_run={cfg.dry_run} strategy={cfg.strategy} resolution={cfg.resolution} "
          f"SL={cfg.atr_stop_mult}xATR target={cfg.reward_risk}R risk/trade={cfg.risk_per_trade:.2%}")
    rm = RiskManager(cfg, path="state/check_state.json")
    for m in markets:
        print(f"\n[{m['symbol']}] product_id={m['pid']} tick={m['tick']} contract_value={m['cval']}")
        df = fetch_closed_candles(client, cfg, m["symbol"])
        d = add_indicators(df, cfg)
        r = d.iloc[-1]
        print(f"candles={len(df)} last closed={fmt_t(int(r['time']))} close={r['close']} "
              f"atr={r['atr']:.2f} rsi={r['rsi']:.1f} trend={'UP' if r['ema_slow'] > r['ema_trend'] else 'DOWN'}")
        sig = signal_at(d, len(d) - 1, cfg)
        print("signal on last closed candle:", f"{sig.side} ({sig.reason})" if sig else "none")
        entry = float(client.get_ticker(m["symbol"]).get("mark_price") or df["close"].iloc[-1])
        for side in ("buy", "sell"):
            sl, tp = levels(side, entry, float(r["atr"]), cfg, m["tick"])
            n = rm.position_size(cfg.bot_capital_usd, entry, sl, m["cval"])
            print(f"  hypothetical {side}: entry~{entry:.2f} SL {sl} TP {tp} size={n} contracts "
                  f"(loss at SL ~{abs(entry - sl) * n * m['cval']:.2f})")
        if cfg.api_key and cfg.api_secret:
            print("  position size:", client.position_size(m["pid"]),
                  "| open orders:", len(client.get_open_orders(m["pid"])))
    if cfg.api_key and cfg.api_secret:
        print(f"\nwallet {cfg.wallet_asset} equity:", client.get_wallet_equity(cfg.wallet_asset))
    else:
        print("\n(no API keys set: skipped private checks)")


def status(cfg):
    rm = RiskManager(cfg)
    print(f"state file: {rm.path}")
    print(json.dumps(rm.state.__dict__, indent=2, default=str))
    path = f"{cfg.log_dir}/{'paper_trades' if cfg.dry_run else 'trades'}.csv"
    if os.path.exists(path):
        t = pd.read_csv(path)
        ex = t[t["event"].isin(["stop", "target"])]
        if len(ex):
            print(f"\nclosed trades={len(ex)} wins={int((ex.pnl > 0).sum())} losses={int((ex.pnl <= 0).sum())} "
                  f"net P&L={ex.pnl.sum():+.2f}")
        print(t.tail(10).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--panic", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    validate(cfg)
    setup_logging(cfg)
    if a.status:
        return status(cfg)
    client = DeltaClient(cfg.base_url, cfg.api_key, cfg.api_secret)
    markets = load_markets(client, cfg)

    if a.check:
        return check(client, cfg, markets)
    if a.panic:
        return panic(client, cfg, markets)

    risk = RiskManager(cfg)
    live_sizes = {}
    if not cfg.dry_run:
        for m in markets:
            try:
                client.set_leverage(m["pid"], cfg.max_leverage)
            except DeltaError as e:
                logging.warning("%s: could not set leverage (verify manually in the Delta UI): %s", m["symbol"], e)
            live_sizes[m["symbol"]] = client.position_size(m["pid"])
    notifier.send(cfg, f"Bot started: {','.join(cfg.symbols)} {cfg.resolution} strategy={cfg.strategy} "
                       f"env={cfg.env} dry_run={cfg.dry_run}")
    errors = 0
    while True:
        try:
            for m in markets:
                step(client, cfg, risk, m, live_sizes)
            errors = 0
        except KeyboardInterrupt:
            notifier.send(cfg, "Bot stopped by user. Open positions keep their exchange-side SL/TP.")
            break
        except Exception as e:
            errors += 1
            logging.exception("loop error")
            if errors in (3, 10):
                notifier.send(cfg, f"Bot errors x{errors}: {e}")
            if errors >= 20:
                notifier.send(cfg, "Too many errors, exiting. Check positions manually on Delta!")
                raise SystemExit(1)                              # non-zero: Docker/systemd restarts it
        if a.once:
            break
        try:
            time.sleep(cfg.poll_seconds)
        except KeyboardInterrupt:
            notifier.send(cfg, "Bot stopped by user. Open positions keep their exchange-side SL/TP.")
            break


if __name__ == "__main__":
    main()
