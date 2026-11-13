"""Risk manager: position sizing, daily loss limit, drawdown kill switch, trade cap. State persisted."""
import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class RiskState:
    day: str = ""
    start_equity: float = 0.0
    peak_equity: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    last_processed: int = 0                                  # legacy (single symbol)
    last_processed_by: dict = field(default_factory=dict)   # symbol -> last closed candle time handled
    paper_equity: float = 0.0                                # DRY_RUN only
    paper_positions: dict = field(default_factory=dict)     # DRY_RUN only: symbol -> open paper position


class RiskManager:
    def __init__(self, cfg, path=None):
        self.cfg = cfg
        self.path = Path(path or cfg.state_path)
        self.state = RiskState()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                known = {f.name for f in fields(RiskState)}
                self.state = RiskState(**{k: v for k, v in raw.items() if k in known})
            except Exception:
                self.state = RiskState()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.state), indent=2))

    def halt(self, reason: str):
        self.state.halted, self.state.halt_reason = True, reason
        self.save()

    def _roll(self, equity: float, now=None):
        today = (now or datetime.now(timezone(timedelta(hours=5, minutes=30)))).strftime("%Y-%m-%d")   # day resets at 00:00 IST
        if self.state.day != today:
            self.state.day, self.state.start_equity, self.state.trades_today = today, equity, 0
        self.state.peak_equity = max(self.state.peak_equity, equity)

    def check(self, equity: float, now=None):
        """Return (allowed, reason). Call only when flat."""
        s, c = self.state, self.cfg
        if s.halted:
            return False, f"HALTED: {s.halt_reason} (delete {self.path} after review to resume)"
        if equity is None or equity <= 0:
            return False, "equity unavailable"
        self._roll(equity, now)
        if (s.peak_equity - equity) / s.peak_equity >= c.max_drawdown:
            self.halt(f"max drawdown {c.max_drawdown:.0%} reached")
            return False, s.halt_reason
        if (s.start_equity - equity) / s.start_equity >= c.daily_loss_limit:
            self.save()
            return False, f"daily loss limit {c.daily_loss_limit:.0%} reached"
        if s.trades_today >= c.max_trades_per_day:
            self.save()
            return False, "max trades per day reached"
        self.save()
        return True, "ok"

    def record_trade(self):
        self.state.trades_today += 1
        self.save()

    def position_size(self, equity: float, entry: float, stop: float, contract_value: float) -> int:
        """Contracts such that hitting the stop loses ~risk_per_trade of capital, capped by leverage."""
        equity = min(equity, self.cfg.bot_capital_usd)
        per_contract_loss = abs(entry - stop) * contract_value
        if per_contract_loss <= 0 or entry <= 0 or contract_value <= 0:
            return 0
        by_risk = math.floor(equity * self.cfg.risk_per_trade / per_contract_loss)
        by_leverage = math.floor(equity * self.cfg.max_leverage / (entry * contract_value))
        return max(0, min(by_risk, by_leverage))
