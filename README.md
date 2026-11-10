# Delta Exchange India Trading Bot

A personal trading bot for **Delta Exchange India** perpetual futures (BTCUSD, ETHUSD). Every trade is sent as a
**bracket order**, with the **stop loss and target stored on the exchange**. If your computer or the bot crashes,
the stop loss still works.

> **Reality check.** This is not a money machine. The backtest shows a **small** edge, and it wins only about **30% of
> trades**. It makes money because winners are 3× the size of losers. Runs of 8–10 losses in a row are normal.
> Only trade money you can afford to lose. Not financial advice: you are responsible for every order it places.

---

## How the bot works (in plain words)

```
every 20 s ──► has a new 4-hour candle CLOSED?  ──no──► wait
                         │ yes
                         ▼
        trend filter: EMA50 > EMA200 and price > EMA200 ?  (uptrend → only BUY)
                      EMA50 < EMA200 and price < EMA200 ?  (downtrend → only SELL)
                         │
                         ▼
        pullback trigger: in an uptrend, RSI dipped below 40 and just closed back above 40 → BUY
                          in a downtrend, RSI rose above 60 and just closed back below 60 → SELL
                         │
                         ▼
        risk checks: not halted, daily loss limit OK, trade cap OK, no open position on this coin
                         │
                         ▼
        STOP LOSS = entry ∓ 1.5 × ATR        TARGET = 3 × stop distance  (risk:reward 1:3)
        SIZE      = contracts so that hitting the stop loses 0.5% of capital
                         │
                         ▼
        ONE market order with SL + TP attached (bracket) ─► bot confirms SL/TP exist, else closes & halts
                         │
                         ▼
        Delta closes the trade by itself when SL or TP is hit → Telegram message
```

The idea: trade **with** the trend and enter **after a dip**, not at random. ATR measures how much the coin normally moves,
so the stop loss sits outside normal noise. It is wider when the market is volatile and tighter when it is calm.

### Safety layer
| Protection | Default | What happens |
|---|---|---|
| Risk per trade | 0.5% of `BOT_CAPITAL_USD` | Size is calculated from the stop distance |
| Daily loss limit | −3% | No new trades until the next UTC day |
| Max drawdown | −10% from peak | Bot **halts permanently** until you reset it by hand |
| Max trades/day | 3 | |
| One position per coin | | New signals are ignored while a position is open |
| Protection check | | If SL/TP can't be confirmed after entry, the bot closes the position and halts |
| No duplicate orders | | Each candle is marked "processed" before ordering, and orders are never auto-retried |

---

## Backtest results (checked 2026-11-10, Dec 2023 – Nov 2026, fees + GST + slippage included)

The strategy was chosen on the **first 60%** of the data and then checked on the **last 40%**, which it had never seen.
193 combinations were tried, and the one with the best tuning-data result was also profitable on unseen data for both coins.

| Coin | Strategy | Trades | Win rate | Profit factor | Return (1% risk) | Max drawdown |
|---|---|---|---|---|---|---|
| BTCUSD | **pullback 4h (new default)** | 99 | 30% | 1.17 | **+14.0%** | 9.2% |
| ETHUSD | **pullback 4h (new default)** | 88 | 31% | 1.23 | **+15.8%** | 11.0% |
| BTCUSD | ema_cross 1h (old) | 228 | 32% | 0.72 | −35.9% | 42.4% |
| ETHUSD | ema_cross 1h (old) | 250 | 28% | 0.66 | −47.1% | 47.1% |

It was profitable in every calendar year on both coins, but BTC in 2025–2026 was only around break-even.
On BTC, small changes to the settings move it to break-even, so the edge is thin. **Don't increase risk because of this table.**

**How often it trades:** about 30 trades per coin per year. With BTC + ETH that is **about 5 trades a month**,
so **10–15 trades take roughly 2–3 months.** Some months have none.

---

## 1. Install (Mac / Linux / Windows)

```bash
git clone https://github.com/Sumit211995/delta_bot.git && cd delta_bot
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
pytest -q tests                                        # 9 tests should pass
```

## 2. Test locally, step by step (no money)

The default `.env` is `DELTA_ENV=live` with `DRY_RUN=1`: **real live prices, paper trades only.**
Open a terminal in the project folder first:
```bash
cd ~/Documents/Project/delta_bot
source .venv/bin/activate              # you'll see (.venv) at the start of the line
```

| What | Command | What you'll see |
|---|---|---|
| Health check | `python bot.py --check` | each coin: price, trend, RSI, today's hypothetical SL/TP/size, wallet (if keys) |
| **Start paper bot (4h)** | `caffeinate -i python bot.py` | "candle … closed: no signal" every 4h; "PAPER BUY/SELL …" when a setup appears |
| Paper bot on 1h | `caffeinate -i python bot.py --resolution 1h` | same, every hour (separate paper record) |
| Paper bot on 30m | `caffeinate -i python bot.py --resolution 30m` | same, every 30 min (separate paper record) |
| **Dashboard (web page)** | `python dashboard.py` then open http://localhost:8000 | signals, SL/TP, chart, open trades, history |
| Results | `python bot.py --status` (add `--resolution 1h` for the 1h record) | paper balance, open trades, wins/losses |
| Backtest | `python backtest.py --days 1000 --symbol ETHUSD --resolution 4h` | past performance + per-year table |
| Stop a running bot/dashboard | `Ctrl + C` in its terminal | open paper trades are kept; it resumes on restart |

Each command that keeps running (bot, dashboard) needs its own terminal tab. In VS Code, use the **+** in the terminal panel.
To compare timeframes, run the 4h, 1h and 30m paper bots side by side and watch their results in the dashboard.
In testing, **only 4h made money after fees**. 30m and 1h lost money (0 of 91 variations worked on unseen data).

Paper records are kept per timeframe in `state/paper_state_<tf>.json` and `logs/paper_trades_<tf>.csv`.
Delete those two files to restart a paper record from zero.

### The dashboard
`python dashboard.py` and then open **http://localhost:8000**. It is **read-only** and never places or cancels orders.
- **Signals now:** a card for every coin × timeframe (30m / 1h / 4h). Each shows BUY / SELL / WAIT, trend, RSI, and in plain words what the
  bot is waiting for. When there's a signal, it shows **Entry, Stop loss, Target, size, and money at risk vs reward**. The orange border marks the
  timeframe the bot actually trades.
- **Chart:** candles, EMA 50 and EMA 200, past BUY/SELL signals, and dashed **Entry / SL / TP lines** for open trades and new signals, plus RSI below.
- **Open trades:** paper trades and your real Delta position (with its SL/TP), with live unrealised P&L.
- **Recent trades:** entries and exits with P&L.

## 3. Get your Delta Exchange API key

1. Log in at **india.delta.exchange** (KYC must be complete).
2. Open your profile menu, go to **API Keys** (under AlgoHub / API management), and click **Create new API key**.
3. Permissions: tick **Read Data** and **Trading**. Never enable withdrawals (the bot doesn't need them).
4. **Whitelist IP:** Delta only accepts API calls from whitelisted IPs. Enter the public IP of the machine that runs the bot
   (your home IP for local testing, then your server's IP once deployed). If a call is rejected, Delta's error message shows the IP to add.
5. Copy the **API key** and **secret** into `.env`. The secret is shown only once. Never share it or commit it.

**Testnet (demo) keys are separate:** create a demo account on Delta's testnet (the link is in Delta's API docs, docs.delta.exchange).
Demo keys only work with `DELTA_ENV=testnet`, and live keys only work with `DELTA_ENV=live`.

## 4. Testnet: real order flow with fake money (do this once)

`.env`: `DELTA_ENV=testnet`, `DRY_RUN=0`, plus your demo key and secret.
```bash
python bot.py --check     # must show a wallet equity number and contract_value (BTCUSD 0.001, ETHUSD 0.01)
python bot.py             # wait for a trade (may take days), or open a small manual position to test --panic
python bot.py --panic     # must cancel orders AND close positions
```
In the Delta UI, check that the position shows **both a stop loss and a take profit**.

## 5. Going live (your 10–15 test trades)

- [ ] Paper mode ran for a while and the behaviour makes sense
- [ ] Testnet: a bracket order showed SL + TP, and `--panic` worked
- [ ] API key: Read + Trading only, IP whitelisted
- [ ] `python bot.py --check` on live shows your wallet equity. **Check whether `WALLET_ASSET` is `USD` or `INR`.**
      `BOT_CAPITAL_USD` must be in the same currency.
- [ ] Telegram alerts arrive (section 8)

`.env`:
```
DELTA_ENV=live
DRY_RUN=0
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
BOT_CAPITAL_USD=500
RISK_PER_TRADE=0.005
```
With $500 and 0.5% risk, each losing trade costs about **$2.50** and each winner makes about **$7.50**.
**Note:** BTCUSD trades in 0.001 BTC contracts, so with small capital the bot may only take 1 contract (or skip the trade).
ETH sizes are finer.

**Judging 10–15 trades:** at a 30% win rate, 10 trades can easily be 2 wins and 8 losses (−$5) or 5 wins (+$25).
15 trades is too few to prove anything. Judge the bot on: did every trade have an SL and TP, did sizes match the risk,
and did the limits work. Judge profit only after 50+ trades.

**Emergency:** `python bot.py --panic`, or close the position in the Delta app.
Ctrl+C stops the bot, but **the open position keeps its SL/TP on the exchange.**
If the bot halts, read `halt_reason` with `python bot.py --status`. Delete `state/risk_state.json` only after you understand why it halted.

---

## 6. Deploy for free (24/7)

The bot needs **a machine that is always on, with a fixed public IP** (for Delta's IP whitelist), and **persistent disk**
for `state/`. It uses about 100 MB of RAM.

| Option | Cost | Fixed IP | Verdict |
|---|---|---|---|
| **Oracle Cloud Always Free VM** (Mumbai/Hyderabad region) | Free forever | Yes | **Recommended** |
| Your own laptop/PC | Free | Home IP (may change) | OK for testing; must stay awake (`caffeinate -i python bot.py` on Mac) |
| Google Cloud e2-micro free tier | Free (US regions) | Check current IPv4 pricing | OK alternative |
| Railway / Render / Fly | Not free for always-on workers (~$5/month) | Not on cheap plans | Not ideal |

### Oracle Cloud Free: step by step
1. Sign up at **cloud.oracle.com** (needs a card for verification; Always Free resources are not charged). Choose home region **India West (Mumbai)** or **India South (Hyderabad)**.
2. **Create a VM instance:** image **Ubuntu 22.04**, shape **VM.Standard.E2.1.Micro** or **Ampere A1** (both "Always Free eligible").
   Download the SSH key. Note the **public IP**.
3. In Delta, **whitelist that public IP** on your API key.
4. Connect and install:
   ```bash
   ssh -i your-key.pem ubuntu@<PUBLIC_IP>
   sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker ubuntu && exit           # log in again after this
   ssh -i your-key.pem ubuntu@<PUBLIC_IP>
   git clone https://github.com/Sumit211995/delta_bot.git && cd delta_bot
   cp .env.example .env && nano .env                 # paste keys, set DRY_RUN etc.
   docker compose up -d --build                      # starts the bot, restarts automatically
   docker compose logs -f delta-bot                  # watch it (Ctrl+C only stops watching)
   ```
5. Useful commands on the server:
   ```bash
   docker compose exec delta-bot python bot.py --status
   docker compose exec delta-bot python bot.py --panic
   git pull && docker compose up -d --build          # update to the latest code
   docker compose down                               # stop the bot (positions keep SL/TP)
   ```
**Dashboard on the server:** it is started too, but only reachable from the server itself (safer). From your laptop run
`ssh -i your-key.pem -L 8000:localhost:8000 ubuntu@<PUBLIC_IP>` and open http://localhost:8000.

`state/` and `logs/` live on the VM's disk (mounted by `docker-compose.yml`), so they survive restarts and updates.

---

## 7. Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DELTA_ENV` | live | `live` (real prices) or `testnet` (demo exchange) |
| `DRY_RUN` | 1 | 1 = paper trading; 0 = real orders |
| `LIVE_CONFIRM` | | must be `I_UNDERSTAND_THE_RISK` for live + `DRY_RUN=0` |
| `DELTA_API_KEY` / `DELTA_API_SECRET` | | API keys (never commit) |
| `WALLET_ASSET` | USD | wallet asset used for equity; confirm with `--check` |
| `SYMBOLS` | BTCUSD,ETHUSD | coins to trade (one position each) |
| `RESOLUTION` | 4h | candle size. Tested on 4h; 1h/2h lost money |
| `STRATEGY` | pullback | `pullback` or `ema_cross` (old) |
| `RSI_PULLBACK` | 40 | pullback trigger level (shorts use 100 − this) |
| `BOT_CAPITAL_USD` | 500 | max capital the bot sizes against |
| `RISK_PER_TRADE` | 0.005 | fraction of capital lost when a stop is hit (hard max 0.02) |
| `MAX_LEVERAGE` | 2 | 1–10 |
| `DAILY_LOSS_LIMIT` | 0.03 | stop for the day |
| `MAX_DRAWDOWN` | 0.10 | permanent halt |
| `MAX_TRADES_PER_DAY` | 3 | across all coins |
| `ATR_STOP_MULT` | 1.5 | stop distance in ATRs |
| `REWARD_RISK` | 3.0 | target = stop distance × this |
| `FEE_RATE` | 0.0006 | per side incl. GST (backtest + paper P&L); check your fee tier |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | | alerts |
| `ML_MODEL_PATH` / `ML_THRESHOLD` | | optional ML filter (`python train_model.py`) |

## 8. Telegram alerts (recommended)
1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `TELEGRAM_TOKEN`.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
   Copy `"chat":{"id": ...}` into `TELEGRAM_CHAT_ID`.
3. You'll get messages for start, every entry (with SL/TP), closes, skips, errors and halts.

## 9. Commands
| Command | Does |
|---|---|
| `python bot.py --check` | connectivity, prices, signals, hypothetical SL/TP/size, wallet balances (if keys) |
| `python bot.py` | run the loop (paper or live, depending on `DRY_RUN`) |
| `python bot.py --resolution 1h` | run on another timeframe (30m, 1h, 2h, 4h); also works with `--status` and `--once` |
| `python bot.py --symbols ETHUSD` | override the coin list |
| `python bot.py --once` | process the latest closed candle once and exit |
| `python bot.py --status` | balance, open trades, wins/losses, last trades |
| `python bot.py --panic` | cancel all orders and close all positions **(also your manual ones on those coins!)** |
| `python dashboard.py` | web dashboard at http://localhost:8000 |
| `python backtest.py --days 1000 --symbol ETHUSD --resolution 4h` | backtest |
| `pytest -q tests` | unit tests |

## 10. India notes
Fees plus 18% GST on fees reduce the edge of frequent trading. That's one reason the bot uses 4h candles.
Ask a CA how crypto-derivative P&L is taxed for you, and export your trade history from Delta regularly.

## Files
| File | Purpose |
|---|---|
| `bot.py` | live/paper loop, `--check`, `--once`, `--status`, `--panic` |
| `strategy.py` | indicators, signals (pullback / ema_cross), SL/TP levels |
| `risk.py` | position sizing, daily loss / drawdown / trade cap, persisted state |
| `paper.py` | paper-trade SL/TP simulation |
| `delta_client.py` | signed Delta REST client (no auto-retry on orders) |
| `backtest.py`, `train_model.py` | research tools |
| `notifier.py` | Telegram alerts |
| `dashboard.py`, `dashboard.html` | read-only web dashboard |
| `Dockerfile`, `docker-compose.yml` | deployment |
| `tests/` | 9 unit tests |
