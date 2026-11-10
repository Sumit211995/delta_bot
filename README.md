# Delta Exchange India Trading Bot

A personal trading bot for **Delta Exchange India** (BTCUSD / ETHUSD perpetuals). It trades with an
EMA trend-following strategy. Every entry is a **bracket order**: the stop loss and take profit sit on
the exchange, so they still work if the bot crashes. It also has hard risk limits and a backtester that
runs the same strategy code as the live loop.

> **Reality check.** No bot guarantees profit. Leveraged derivatives can lose money fast. Only trade money
> you can afford to lose completely. This is not financial advice. You are responsible for every order the bot places.

---

## Current backtest results (checked 2026-11-10, default settings)

| Market | Timeframe | Trades (2 yrs) | Win rate | Profit factor | Return | Max drawdown |
|---|---|---|---|---|---|---|
| BTCUSD | 1h (default) | 175 | 34.3% | 0.86 | **−8.3%** | 14.1% |
| ETHUSD | 1h | 198 | 31.3% | 0.80 | **−14.0%** | 17.1% |
| BTCUSD | 4h | 37 | 37.8% | 1.10 | +1.2% | 4.3% |
| ETHUSD | 4h | 38 | 26.3% | 0.67 | −4.7% | 6.9% |

**With default settings, the strategy currently loses money after fees.** A profit factor below 1.0 means it loses.
BTCUSD 4h is roughly break-even, but 37 trades are too few to trust. Treat the strategy as a starting point to improve.
The order execution and risk layer are the parts that are ready to use.

How often it trades: on BTCUSD 1h the bot averages about **1 trade every 4 days**. On 4h it averages about 1 trade every 3 weeks.
**10–15 trades take roughly 6–8 weeks on 1h.** The bot does not trade every day.

---

## How a trade works

1. Every 20 seconds the bot checks whether a new candle has **closed**. It never uses an unfinished candle.
2. **Buy signal:** EMA20 crosses above EMA50, price is above EMA200, and RSI < 70.
   **Sell signal:** EMA20 crosses below EMA50, price is below EMA200, and RSI > 30.
3. **Stop loss** = entry ± 1.5 × ATR(14). **Target** = 2 × the stop distance, giving 1:2 risk:reward.
4. **Size** is set so that hitting the stop loses about `RISK_PER_TRADE` of capital (default 0.5%). Leverage also caps the size.
5. The bot sends one market order with the stop loss and target attached (trigger = mark price).
6. The bot then confirms the protective orders exist. If it can't confirm them, it **closes the position and halts**.

Risk limits are saved in `state/risk_state.json`:
- Daily loss limit (default −3%): no new trades for the rest of the day (UTC).
- Max drawdown from peak (default −10%): the bot halts **permanently** until you reset it by hand.
- Max trades per day (default 3). One position at a time.

---

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # edit values. NEVER commit .env
pytest -q tests                                      # 7 tests should pass
```
Keep your system clock synced (NTP). Delta rejects request signatures older than about 5 seconds.

## 2. Backtest (no API keys needed)

```bash
DELTA_ENV=live python backtest.py --days 730
DELTA_ENV=live SYMBOL=ETHUSD RESOLUTION=4h python backtest.py --days 730
```
Check the profit factor (it must be > 1.2 to be worth anything), max drawdown, and number of trades (you want 100+).
If a setting looks good on only one period or one coin, it is overfitted.

## 3. Paper trade on live prices (no keys, no orders)

```bash
DELTA_ENV=live DRY_RUN=1 python bot.py --check   # product info, current signal, hypothetical SL/TP/size
DELTA_ENV=live DRY_RUN=1 python bot.py           # logs the orders it WOULD place to logs/trades.csv
```
Note: dry-run logs every signal. It does not simulate "a position is already open" or the daily trade cap.

## 4. Testnet (real order flow, fake money). Do this before going live.

Create keys at testnet.delta.exchange (permissions: Read + Trade). Set `DELTA_ENV=testnet`, `DRY_RUN=0`, then run:
```bash
python bot.py --check     # wallet equity must show a sensible number for WALLET_ASSET; contract_value=0.001 for BTCUSD
python bot.py             # wait for a trade
python bot.py --panic     # confirm this cancels orders AND closes the position
```
In the Delta UI, check that the position shows **both a stop loss and a take profit**, and that the leverage is set.

## 5. Going live (small first trades)

Checklist:
- [ ] Testnet: a bracket order showed SL + TP in the UI, and `--panic` worked
- [ ] API key: **Read + Trade only, withdrawals disabled**, IP whitelist set (your VPS IP)
- [ ] `--check` on live shows correct wallet equity. **Confirm whether `WALLET_ASSET` is `USD` or `INR`.**
      `BOT_CAPITAL_USD` is compared against that number, so both must use the same currency.
- [ ] `BOT_CAPITAL_USD` small, `RISK_PER_TRADE=0.005`, `MAX_LEVERAGE` 1–2
- [ ] Telegram alerts arrive

Then set the following in `.env`:
```
DELTA_ENV=live
DRY_RUN=0
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```
For a first test of 10–15 live trades: with `BOT_CAPITAL_USD=500` and 0.5% risk, each losing trade costs about **$2.50**.
10 losses in a row (which the backtest shows can happen) cost about $25. After 15 trades, compare the win rate and average R
in `logs/trades.csv` and on Delta against the backtest before you increase size.

Emergency stop: `python bot.py --panic` (or close the position in the Delta app). Stopping the bot with Ctrl+C
**leaves an open position in place, protected by its exchange-side SL/TP.**

If the bot halts, read `halt_reason` in `state/risk_state.json`. Delete that file only after you understand why it halted.

---

## 6. Deploy (24/7)

The bot must keep running, and it **must keep the `state/` folder between restarts**. That folder holds the halt flag and the
last processed candle; if it is lost, the risk limits reset.

**Docker / VPS (recommended):**
```bash
docker compose up -d --build     # uses .env, restart: unless-stopped, state/ and logs/ mounted on the host
docker compose logs -f
```

**Railway:** deploy from the GitHub repo (it builds the `Dockerfile`), add the `.env` values as service variables,
and **attach a volume mounted at `/app/state`**. Without the volume, every redeploy wipes the risk state.
Delta IP whitelisting needs a fixed outbound IP. Railway does not give one by default, so a small VPS is simpler.

---

## 7. Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DELTA_ENV` | testnet | `testnet` or `live` |
| `DRY_RUN` | 1 | 1 = log only; 0 = real orders |
| `LIVE_CONFIRM` | | must be `I_UNDERSTAND_THE_RISK` for live + DRY_RUN=0 |
| `DELTA_API_KEY` / `DELTA_API_SECRET` | | API keys (never commit) |
| `WALLET_ASSET` | USD | asset used for equity; confirm with `--check` |
| `SYMBOL` | BTCUSD | product symbol |
| `RESOLUTION` | 1h | 15m, 30m, 1h, 2h, 4h, 1d |
| `BOT_CAPITAL_USD` | 500 | max capital the bot sizes against |
| `RISK_PER_TRADE` | 0.005 | fraction lost if the stop is hit (max 0.02) |
| `MAX_LEVERAGE` | 2 | 1–10 |
| `DAILY_LOSS_LIMIT` | 0.03 | stop for the day |
| `MAX_DRAWDOWN` | 0.10 | permanent halt |
| `MAX_TRADES_PER_DAY` | 3 | |
| `ATR_STOP_MULT` | 1.5 | stop distance in ATRs |
| `REWARD_RISK` | 2.0 | target = stop distance × this |
| `FEE_RATE` | 0.0005 | per side (backtest only); check your fee tier |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | | alerts |
| `ML_MODEL_PATH` / `ML_THRESHOLD` | | optional ML filter |

## 8. Optional ML filter
`python train_model.py --days 1000` trains a filter on the strategy's past signals (chronological split). It saves the
model only if the model improves the out-of-sample win rate. Usually it won't, and that is an honest result.

## 9. India notes
Fees plus 18% GST on fees reduce the edge of frequent trading. Ask a CA how crypto derivative P&L is taxed for you,
and export your trade history regularly.

## Files
| File | Purpose |
|---|---|
| `bot.py` | live loop, `--check`, `--panic` |
| `strategy.py` | indicators, signals, SL/TP levels, ML hook |
| `risk.py` | position sizing, daily loss / drawdown / trade cap |
| `delta_client.py` | signed REST client (no auto-retry on orders) |
| `backtest.py` / `train_model.py` | research tools |
| `notifier.py` | Telegram alerts |
| `Dockerfile`, `docker-compose.yml` | deployment |
| `tests/` | 7 unit tests |
