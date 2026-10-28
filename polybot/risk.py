"""Risk manager: every order goes through `allow_buy`. The kill switch trips when equity
falls more than DAILY_LOSS_LIMIT_USD below the start-of-day value."""

import logging

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


class Risk:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db

    @property
    def paused(self) -> bool:
        return bool(self.db.get("paused", False))

    @property
    def killed(self) -> bool:
        return bool(self.db.get("killed", False))

    def pause(self, reason: str) -> None:
        self.db.set("paused", True)
        self.db.set("pause_reason", reason)
        self.db.event("warn", f"paused: {reason}")

    def resume(self) -> None:
        self.db.set("paused", False)
        self.db.set("killed", False)
        self.db.event("info", "resumed")

    def kill(self, reason: str) -> None:
        self.db.set("killed", True)
        self.pause(reason)

    def allow_buy(self, usd: float, exposure: float, cash: float) -> tuple[bool, str]:
        if self.paused:
            return False, "bot is paused"
        if usd <= 0:
            return False, "zero size"
        if exposure + usd > self.s.max_total_exposure_usd:
            return False, f"exposure cap (${exposure:.2f} + ${usd:.2f} > ${self.s.max_total_exposure_usd:.2f})"
        if cash - usd < self.s.min_cash_reserve_usd:
            return False, f"cash reserve (${cash:.2f} free)"
        return True, ""

    def check_drawdown(self, equity: float) -> str | None:
        """Returns a reason string if the kill switch should trip."""
        start = self.db.update_equity(equity)
        loss = start - equity
        if loss >= self.s.daily_loss_limit_usd and not self.killed:
            return f"daily loss ${loss:.2f} hit limit ${self.s.daily_loss_limit_usd:.2f}"
        return None
