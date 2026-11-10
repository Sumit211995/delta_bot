"""Configuration loaded from environment / .env. Safe defaults: testnet + dry run."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_URLS = {
    "live": "https://api.india.delta.exchange",
    "testnet": "https://cdn-ind.testnet.deltaex.org",
}
RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                      "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}
LIVE_PHRASE = "I_UNDERSTAND_THE_RISK"


def _load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    env: str = "testnet"
    dry_run: bool = True
    live_confirm: str = ""
    api_key: str = ""
    api_secret: str = ""
    wallet_asset: str = "USD"
    symbol: str = "BTCUSD"                    # first of `symbols` (used by backtest/train)
    symbols: tuple = ("BTCUSD",)
    resolution: str = "4h"
    paper_timeframes: tuple = ()             # extra timeframes paper-traded alongside RESOLUTION (never real orders)
    paper_trail_timeframes: tuple = ()       # timeframes ALSO paper-traded with a trailing stop instead of a fixed target
    exit_mode: str = "fixed"                  # fixed = SL + TP (bracket) | trail = SL only, trails after +TRAIL_ACTIVATE_R
    trail_activate_r: float = 1.0             # start trailing once the trade is this many R in profit (on a close)
    trail_atr: float = 2.0                    # trailing distance: this many ATRs behind the best close
    strategy: str = "pullback"                # pullback | ema_cross
    rsi_pullback: float = 40.0
    bot_capital_usd: float = 500.0
    risk_per_trade: float = 0.005
    max_leverage: int = 2
    daily_loss_limit: float = 0.03
    max_drawdown: float = 0.10
    max_trades_per_day: int = 3
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    reward_risk: float = 3.0
    fee_rate: float = 0.0006                  # taker 0.05% + 18% GST, per side
    slippage_bps: float = 2.0
    poll_seconds: int = 20
    telegram_token: str = ""
    telegram_chat_id: str = ""
    ml_model_path: str = ""
    ml_threshold: float = 0.55
    state_file: str = ""                      # override; default depends on mode (see state_path)
    log_dir: str = "logs"

    @property
    def book(self) -> str:
        """Name of this paper record: '4h', '1h', ... or '4h-trail' for the trailing-stop variant."""
        return self.resolution + ("-trail" if self.exit_mode == "trail" else "")

    @property
    def state_path(self) -> str:
        """Live has ONE state file. Paper keeps one per book so 30m/1h/4h/4h-trail can be compared side by side."""
        if self.state_file:
            return self.state_file
        return f"state/paper_state_{self.book}.json" if self.dry_run else "state/risk_state.json"

    @property
    def trades_path(self) -> str:
        return f"{self.log_dir}/paper_trades_{self.book}.csv" if self.dry_run else f"{self.log_dir}/trades.csv"

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]

    @property
    def period_seconds(self) -> int:
        return RESOLUTION_SECONDS[self.resolution]


def load_config() -> Config:
    _load_env()
    g = os.getenv
    raw = g("SYMBOLS") or g("SYMBOL") or "BTCUSD"
    symbols = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    dry = g("DRY_RUN", "1") == "1"
    return Config(
        env=g("DELTA_ENV", "testnet").lower(),
        dry_run=dry,
        live_confirm=g("LIVE_CONFIRM", ""),
        api_key=g("DELTA_API_KEY", ""),
        api_secret=g("DELTA_API_SECRET", ""),
        wallet_asset=g("WALLET_ASSET", "USD"),
        symbol=symbols[0],
        symbols=symbols,
        resolution=g("RESOLUTION", "4h"),
        paper_timeframes=tuple(x.strip() for x in g("PAPER_TIMEFRAMES", "").split(",") if x.strip()),
        paper_trail_timeframes=tuple(x.strip() for x in g("PAPER_TRAIL_TIMEFRAMES", "").split(",") if x.strip()),
        trail_activate_r=float(g("TRAIL_ACTIVATE_R", "1.0")),
        trail_atr=float(g("TRAIL_ATR", "2.0")),
        strategy=g("STRATEGY", "pullback").lower(),
        rsi_pullback=float(g("RSI_PULLBACK", "40")),
        bot_capital_usd=float(g("BOT_CAPITAL_USD", "500")),
        risk_per_trade=float(g("RISK_PER_TRADE", "0.005")),
        max_leverage=int(g("MAX_LEVERAGE", "2")),
        daily_loss_limit=float(g("DAILY_LOSS_LIMIT", "0.03")),
        max_drawdown=float(g("MAX_DRAWDOWN", "0.10")),
        max_trades_per_day=int(g("MAX_TRADES_PER_DAY", "3")),
        atr_stop_mult=float(g("ATR_STOP_MULT", "1.5")),
        reward_risk=float(g("REWARD_RISK", "3.0")),
        fee_rate=float(g("FEE_RATE", "0.0006")),
        telegram_token=g("TELEGRAM_TOKEN", ""),
        telegram_chat_id=g("TELEGRAM_CHAT_ID", ""),
        ml_model_path=g("ML_MODEL_PATH", ""),
        ml_threshold=float(g("ML_THRESHOLD", "0.55")),
    )


def validate(cfg: Config) -> None:
    """Refuse to start in unsafe or inconsistent configurations."""
    if cfg.env not in BASE_URLS:
        raise SystemExit("DELTA_ENV must be 'testnet' or 'live'")
    if cfg.resolution not in RESOLUTION_SECONDS:
        raise SystemExit(f"RESOLUTION must be one of {list(RESOLUTION_SECONDS)}")
    if cfg.exit_mode not in ("fixed", "trail"):
        raise SystemExit("exit_mode must be 'fixed' or 'trail'")
    if cfg.exit_mode == "trail" and not cfg.dry_run:
        raise SystemExit("The trailing-stop exit is PAPER ONLY for now (live orders use a fixed SL + TP bracket)")
    if cfg.trail_activate_r < 0 or cfg.trail_atr <= 0:
        raise SystemExit("TRAIL_ACTIVATE_R must be >= 0 and TRAIL_ATR > 0")
    bad = [tf for tf in cfg.paper_timeframes + cfg.paper_trail_timeframes if tf not in RESOLUTION_SECONDS]
    if bad:
        raise SystemExit(f"PAPER_TIMEFRAMES / PAPER_TRAIL_TIMEFRAMES has unknown timeframe(s) {bad}; use {list(RESOLUTION_SECONDS)}")
    if cfg.strategy not in ("pullback", "ema_cross"):
        raise SystemExit("STRATEGY must be 'pullback' or 'ema_cross'")
    if not cfg.symbols:
        raise SystemExit("SYMBOLS is empty")
    if cfg.atr_stop_mult <= 0 or cfg.reward_risk <= 0:
        raise SystemExit("ATR_STOP_MULT and REWARD_RISK must be > 0")
    if not 0 < cfg.risk_per_trade <= 0.02:
        raise SystemExit("RISK_PER_TRADE must be between 0 and 0.02 (2%)")
    if cfg.max_leverage < 1 or cfg.max_leverage > 10:
        raise SystemExit("MAX_LEVERAGE must be 1..10")
    if not cfg.dry_run:
        if not (cfg.api_key and cfg.api_secret):
            raise SystemExit("DELTA_API_KEY / DELTA_API_SECRET required when DRY_RUN=0")
        if cfg.env == "live" and cfg.live_confirm != LIVE_PHRASE:
            raise SystemExit(f"Live trading needs LIVE_CONFIRM={LIVE_PHRASE}")
