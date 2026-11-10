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
    symbol: str = "BTCUSD"
    resolution: str = "1h"
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
    reward_risk: float = 2.0
    fee_rate: float = 0.0005
    slippage_bps: float = 2.0
    poll_seconds: int = 20
    telegram_token: str = ""
    telegram_chat_id: str = ""
    ml_model_path: str = ""
    ml_threshold: float = 0.55
    state_file: str = "state/risk_state.json"
    log_dir: str = "logs"

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]

    @property
    def period_seconds(self) -> int:
        return RESOLUTION_SECONDS[self.resolution]


def load_config() -> Config:
    _load_env()
    g = os.getenv
    return Config(
        env=g("DELTA_ENV", "testnet").lower(),
        dry_run=g("DRY_RUN", "1") == "1",
        live_confirm=g("LIVE_CONFIRM", ""),
        api_key=g("DELTA_API_KEY", ""),
        api_secret=g("DELTA_API_SECRET", ""),
        wallet_asset=g("WALLET_ASSET", "USD"),
        symbol=g("SYMBOL", "BTCUSD"),
        resolution=g("RESOLUTION", "1h"),
        bot_capital_usd=float(g("BOT_CAPITAL_USD", "500")),
        risk_per_trade=float(g("RISK_PER_TRADE", "0.005")),
        max_leverage=int(g("MAX_LEVERAGE", "2")),
        daily_loss_limit=float(g("DAILY_LOSS_LIMIT", "0.03")),
        max_drawdown=float(g("MAX_DRAWDOWN", "0.10")),
        max_trades_per_day=int(g("MAX_TRADES_PER_DAY", "3")),
        atr_stop_mult=float(g("ATR_STOP_MULT", "1.5")),
        reward_risk=float(g("REWARD_RISK", "2.0")),
        fee_rate=float(g("FEE_RATE", "0.0005")),
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
    if not 0 < cfg.risk_per_trade <= 0.02:
        raise SystemExit("RISK_PER_TRADE must be between 0 and 0.02 (2%)")
    if cfg.max_leverage < 1 or cfg.max_leverage > 10:
        raise SystemExit("MAX_LEVERAGE must be 1..10")
    if not cfg.dry_run:
        if not (cfg.api_key and cfg.api_secret):
            raise SystemExit("DELTA_API_KEY / DELTA_API_SECRET required when DRY_RUN=0")
        if cfg.env == "live" and cfg.live_confirm != LIVE_PHRASE:
            raise SystemExit(f"Live trading needs LIVE_CONFIRM={LIVE_PHRASE}")
