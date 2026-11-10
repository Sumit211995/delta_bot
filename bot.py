"""Live trading loop for Delta Exchange India.

  python bot.py --check     # connectivity, product info, balance, position, hypothetical signal. No orders.
  python bot.py             # run (DRY_RUN=1: logs the orders it WOULD send; DRY_RUN=0: sends real orders)
  python bot.py --panic     # cancel all orders and close the position now
"""
import argparse
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

import notifier
from config import load_config, validate
from delta_client import DeltaClient, DeltaError
from risk import RiskManager
from strategy import add_indicators, levels, ml_pass, signal_at


def setup_logging(cfg):
    os.makedirs(cfg.log_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(f"{cfg.log_dir}/bot.log"), logging.StreamHandler()])


def fetch_closed_candles(client, cfg, bars=400) -> pd.DataFrame:
    now = int(time.time())
    rows = client.get_candles(cfg.symbol, cfg.resolution, now - (bars + 5) * cfg.period_seconds, now)
    df = pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]].astype(float)
    df = df[df["time"] + cfg.period_seconds <= now]            # drop the still-forming candle
    return df.reset_index(drop=True)


def log_trade(cfg, row: dict):
    os.makedirs(cfg.log_dir, exist_ok=True)
    path = f"{cfg.log_dir}/trades.csv"
    pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.exists(path), index=False)


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


def panic(client, cfg, pid):
    client.cancel_all(pid)
    res = client.close_position(pid)
    notifier.send(cfg, f"PANIC executed on {cfg.symbol}: {res}")


def step(client, cfg, risk, pid, tick, cval):
    df = fetch_closed_candles(client, cfg)
    last_t = int(df["time"].iloc[-1])
    if last_t <= risk.state.last_processed:
        return
    risk.state.last_processed = last_t                          # mark first: never double-trade a candle
    risk.save()
    d = add_indicators(df, cfg)
    sig = signal_at(d, len(d) - 1, cfg)
    ts = datetime.fromtimestamp(last_t, timezone.utc).strftime("%Y-%m-%d %H:%M")
    if sig is None:
        logging.info("candle %s closed: no signal", ts)
        return
    if not ml_pass(sig, cfg):
        logging.info("candle %s: %s signal rejected by ML filter", ts, sig.side)
        return

    live = not cfg.dry_run
    if live and client.position_size(pid) != 0:
        logging.info("signal ignored: position already open")
        return
    equity = cfg.bot_capital_usd
    if live:
        equity = client.get_wallet_equity(cfg.wallet_asset)
        if equity is None:
            notifier.send(cfg, f"Wallet asset {cfg.wallet_asset} not found; no trade (fail-safe).")
            return
    ok, why = risk.check(equity)
    if not ok:
        notifier.send(cfg, f"Signal {sig.side} skipped: {why}")
        return

    entry = float(client.get_ticker(cfg.symbol).get("mark_price") or df["close"].iloc[-1])
    sl, tp = levels(sig.side, entry, sig.atr, cfg, tick)
    size = risk.position_size(equity, entry, sl, cval)
    if size < 1:
        notifier.send(cfg, f"Signal {sig.side} skipped: size < 1 contract (capital too small for this stop).")
        return

    msg = (f"{'LIVE' if live else 'DRY-RUN'} {sig.side.upper()} {size} {cfg.symbol} @~{entry} "
           f"SL {sl} TP {tp} | {sig.reason}")
    notifier.send(cfg, msg)
    row = dict(time=ts, side=sig.side, size=size, entry=entry, sl=sl, tp=tp, live=live)
    if not live:
        log_trade(cfg, row)
        return
    try:
        res = client.place_bracket_market(pid, size, sig.side, sl, tp, uuid.uuid4().hex[:24])
    except DeltaError as e:
        notifier.send(cfg, f"ORDER FAILED: {e}")
        return
    risk.record_trade()
    log_trade(cfg, {**row, "order_id": res.get("result", {}).get("id")})
    if not protection_ok(client, pid):
        panic(client, cfg, pid)
        risk.halt("no protective order detected after entry")
        notifier.send(cfg, "Position flattened and bot HALTED: protective order not confirmed.")


def check(client, cfg, pid, tick, cval):
    print(f"env={cfg.env} dry_run={cfg.dry_run} symbol={cfg.symbol} product_id={pid} tick={tick} contract_value={cval}")
    df = fetch_closed_candles(client, cfg)
    d = add_indicators(df, cfg)
    print(f"candles={len(df)} last_close={df['close'].iloc[-1]} atr={d['atr'].iloc[-1]:.2f}")
    sig = signal_at(d, len(d) - 1, cfg)
    print("signal on last closed candle:", sig.side if sig else "none")
    entry = float(client.get_ticker(cfg.symbol).get("mark_price") or df["close"].iloc[-1])
    for side in ("buy", "sell"):
        sl, tp = levels(side, entry, float(d["atr"].iloc[-1]), cfg, tick)
        n = RiskManager(cfg, path="state/check_state.json").position_size(cfg.bot_capital_usd, entry, sl, cval)
        print(f"hypothetical {side}: entry~{entry} SL {sl} TP {tp} size={n} contracts")
    if cfg.api_key and cfg.api_secret:
        print(f"wallet {cfg.wallet_asset} equity:", client.get_wallet_equity(cfg.wallet_asset))
        print("position size:", client.position_size(pid))
        print("open orders:", len(client.get_open_orders(pid)))
    else:
        print("(no API keys set: skipped private checks)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--panic", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    validate(cfg)
    setup_logging(cfg)
    client = DeltaClient(cfg.base_url, cfg.api_key, cfg.api_secret)
    product = client.get_product(cfg.symbol)
    pid, tick, cval = int(product["id"]), float(product["tick_size"]), float(product["contract_value"])

    if a.check:
        return check(client, cfg, pid, tick, cval)
    if a.panic:
        return panic(client, cfg, pid)

    risk = RiskManager(cfg)
    if not cfg.dry_run:
        try:
            client.set_leverage(pid, cfg.max_leverage)
        except DeltaError as e:
            logging.warning("could not set leverage (verify manually in the Delta UI): %s", e)
    notifier.send(cfg, f"Bot started: {cfg.symbol} {cfg.resolution} env={cfg.env} dry_run={cfg.dry_run}")
    errors = 0
    while True:
        try:
            step(client, cfg, risk, pid, tick, cval)
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
                break
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
