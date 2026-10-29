"""Entry point: python -m polybot.main"""

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import uvicorn

from .analyst import Analyst
from .config import Settings
from .dashboard import build_app
from .db import DB, utc_day
from .exchange import make_exchange
from .forecaster import Forecaster
from .market_maker import MarketMaker
from .news import RSSSource, XSource, load_feeds
from .risk import Risk
from .telegram import Telegram

log = logging.getLogger("polybot")


def setup_logging(level: str) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in (logging.StreamHandler(sys.stdout),
              RotatingFileHandler("logs/polybot.log", maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def check_geoblock() -> dict:
    """Polymarket's own endpoint says whether this server's IP may trade."""
    try:
        return httpx.get("https://polymarket.com/api/geoblock", timeout=10).json()
    except Exception as e:
        return {"error": str(e)}


class Bot:
    def __init__(self, s: Settings):
        self.s = s
        self.db = DB(s.db_path)
        self.ex = make_exchange(s, self.db)
        self.risk = Risk(s, self.db)
        self.tg = Telegram(s.telegram_bot_token, s.telegram_chat_id)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mm = MarketMaker(s, self.db, self.ex, self.risk, self.notify) if s.mm_enabled else None
        self.fc = None
        if s.fc_enabled:
            if not s.anthropic_api_key:
                log.warning("FC_ENABLED but no ANTHROPIC_API_KEY; forecaster disabled")
            else:
                x = XSource(s.x_bearer_token, s.x_account_list, self.db) if s.x_bearer_token and s.x_account_list else None
                self.fc = Forecaster(s, self.db, self.ex, self.risk, Analyst(s, self.db),
                                     RSSSource(load_feeds(s.feeds_file)), x, self.notify)
        self._last_error_alert = 0.0

    # --- notifications (callable from worker threads) --------------------------------------
    def notify(self, text: str) -> None:
        self.db.event("info", text)
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.tg.send(text), self.loop)

    def error(self, where: str, e: Exception) -> None:
        log.exception("%s failed", where)
        self.db.event("error", f"{where}: {e}")
        if time.time() - self._last_error_alert > 900:  # at most one error alert per 15 min
            self._last_error_alert = time.time()
            self.notify(f"⚠️ {where} error: {e}")

    # --- loops ------------------------------------------------------------------------
    def core_tick(self) -> None:
        for f in self.ex.sync_fills():
            q = self.question(f["market"])
            self.notify(f"{'🟢' if f['side'] == 'BUY' else '🔴'} [{f['strategy']}] {f['side']} "
                        f"{f['size']:g} {f['outcome']} @ {f['price']:.3f} — {q}")
        reason = self.risk.check_drawdown(self.ex.equity())
        if reason:
            self.ex.cancel_all()
            self.risk.kill(reason)
            self.notify(f"🚨 KILL SWITCH: {reason}. All orders cancelled. Send /resume to restart.")
        if self.mm:
            self.mm.step()  # when paused this only pulls quotes and runs stop-losses

    async def run_every(self, name: str, seconds: int, fn) -> None:
        while True:
            try:
                await asyncio.to_thread(fn)
            except Exception as e:
                self.error(name, e)
            await asyncio.sleep(seconds)

    async def daily_summary(self) -> None:
        while True:
            now = time.gmtime()
            hour = self.s.summary_hour_utc
            wait = ((hour - now.tm_hour) % 24) * 3600 - now.tm_min * 60 - now.tm_sec
            await asyncio.sleep(wait if wait > 0 else wait + 86400)
            try:
                self.notify(await asyncio.to_thread(self.summary))
            except Exception as e:
                self.error("daily summary", e)

    def reconcile(self) -> None:
        if hasattr(self.ex, "reconcile_positions"):
            self.ex.reconcile_positions()
        if hasattr(self.ex, "rewards_paid"):
            for day in (utc_day(time.time() - 86400), utc_day()):
                self.db.set_reward("real", self.ex.rewards_paid(day), day)

    # --- state for dashboard / telegram ----------------------------------------------
    def question(self, market: str) -> str:
        return self.db.get(f"q:{market}") or market[:12]

    def state(self) -> dict:
        positions = self.db.positions()
        mids = self.ex.midpoints([p.token_id for p in positions]) if positions else {}
        cash = self.ex.cash()
        equity = self.ex.equity()
        start = self.db.q("SELECT start_equity FROM equity WHERE day=?", (utc_day(),))
        return {
            "mode": self.s.mode,
            "paused": self.risk.paused,
            "killed": self.risk.killed,
            "cash": cash,
            "equity": equity,
            "day_pnl": equity - (start[0]["start_equity"] if start else equity),
            "realized": self.db.get("realized_pnl", 0.0),
            "exposure": self.ex.exposure(),
            "claude_today": self.db.claude_spend_today(),
            "rewards_est_today": self.db.rewards("est")[0],
            "rewards_est_total": self.db.rewards("est")[1],
            "rewards_real_today": self.db.rewards("real")[0],
            "rewards_real_total": self.db.rewards("real")[1],
            "positions": [{
                "question": self.question(p.market), "outcome": p.outcome, "strategy": p.strategy,
                "size": p.size, "avg_price": p.avg_price, "mid": mids.get(p.token_id),
                "pnl": (mids.get(p.token_id, p.avg_price) - p.avg_price) * p.size,
            } for p in positions],
            "orders": [{"question": self.question(o.market), "side": o.side, "outcome": o.outcome,
                        "price": o.price, "size": o.size, "strategy": o.strategy}
                       for o in self.ex.open_orders()],
            "signals": [dict(r) for r in self.db.q("SELECT * FROM signals ORDER BY ts DESC LIMIT 30")],
            "fills": [dict(r) for r in self.db.q("SELECT * FROM fills ORDER BY ts DESC LIMIT 30")],
            "events": [dict(r) for r in self.db.q("SELECT * FROM events ORDER BY ts DESC LIMIT 40")],
        }

    def summary(self) -> str:
        st = self.state()
        lines = [
            f"📊 polybot ({st['mode']}) {'PAUSED' if st['paused'] else 'running'}",
            f"Equity ${st['equity']:.2f} | cash ${st['cash']:.2f} | today {st['day_pnl']:+.2f}",
            f"Realized P&L ${st['realized']:+.2f} | exposure ${st['exposure']:.2f}",
            f"Rewards today: est ${st['rewards_est_today']:.2f}"
            + (f", paid ${st['rewards_real_today']:.2f}" if self.s.live else "")
            + f" | all time: est ${st['rewards_est_total']:.2f}"
            + (f", paid ${st['rewards_real_total']:.2f}" if self.s.live else ""),
            f"Net (realized + est. rewards) ${st['realized'] + st['rewards_est_total']:+.2f}",
            f"Claude spend today ${st['claude_today']:.2f}",
            f"{len(st['positions'])} positions, {len(st['orders'])} open orders",
        ]
        return "\n".join(lines)

    def positions_text(self) -> str:
        st = self.state()
        if not st["positions"]:
            return "No open positions."
        return "\n".join(f"• {p['outcome']} {p['size']:.1f} @ {p['avg_price']:.3f} → "
                         f"{(p['mid'] or 0):.3f} ({p['pnl']:+.2f}) {p['question'][:60]}"
                         for p in st["positions"])

    # --- actions -----------------------------------------------------------------------
    def do_pause(self) -> str:
        self.risk.pause("manual")
        self.ex.cancel_all()
        return "⏸ Paused. Open orders cancelled; positions kept."

    def do_resume(self) -> str:
        self.risk.resume()
        return "▶️ Resumed."

    def shutdown(self) -> str:
        """Cancel the bot's orders and, if enabled, sell what it holds. Safe to call twice."""
        lines = []
        try:
            self.ex.cancel_all()
            lines.append("orders cancelled")
        except Exception as e:
            lines.append(f"could not cancel orders: {e}")
        if self.s.close_positions_on_shutdown:
            positions = [p for p in self.db.positions() if p.strategy in ("mm", "fc")]
            books = self.ex.books([p.token_id for p in positions]) if positions else {}
            for p in positions:
                b = books.get(p.token_id)
                if not b or b.best_bid is None:
                    lines.append(f"no buyers for {p.outcome} in {self.question(p.market)[:40]}, still held")
                    continue
                try:
                    self.ex.take(token_id=p.token_id, side="SELL", size=p.size, limit_price=b.best_bid,
                                 strategy=p.strategy, market=p.market, outcome=p.outcome)
                    self.ex.sync_fills()
                    lines.append(f"sold {p.size:g} {p.outcome} @ {b.best_bid:.3f} "
                                 f"({(b.best_bid - p.avg_price) * p.size:+.2f})")
                except Exception as e:
                    lines.append(f"could not sell {p.outcome} in {self.question(p.market)[:40]}: {e}")
            left = [p for p in self.db.positions() if p.strategy in ("mm", "fc")]
            if left:
                lines.append(f"{len(left)} position(s) still held")
        log.info("shutdown: %s", "; ".join(lines))
        return "🛑 Bot stopped: " + "; ".join(lines)

    def do_kill(self) -> str:
        self.ex.cancel_all()
        self.risk.kill("manual kill switch")
        return "🛑 Killed: all orders cancelled, trading stopped. /resume to restart."

    async def main(self) -> None:
        self.loop = asyncio.get_running_loop()
        for cmd, fn in {"status": self.summary, "positions": self.positions_text,
                        "pause": self.do_pause, "resume": self.do_resume, "kill": self.do_kill}.items():
            self.tg.on(cmd, lambda _args, fn=fn: asyncio.to_thread(fn))
        stop_requested = asyncio.Event()

        async def stop(_args):
            stop_requested.set()
            return "Stopping: cancelling orders and closing positions..."
        self.tg.on("stop", stop)

        # Deadline (unix time) for stopping by itself; 0 = run until told to stop.
        self._stop_at = time.time() + self.s.run_for_minutes * 60 if self.s.run_for_minutes > 0 else 0.0

        async def stopin(args):
            try:
                minutes = float(args.split()[0])
            except (IndexError, ValueError):
                return "Usage: /stopin 45  (minutes from now; /stopin 0 cancels the timer)"
            self._stop_at = time.time() + minutes * 60 if minutes > 0 else 0.0
            return f"⏱ Will stop in {minutes:g} min." if minutes > 0 else "⏱ Timer off: running until /stop."
        self.tg.on("stopin", stopin)

        async def help_text(_args):
            return "Commands: /status /positions /pause /resume /kill /stop /stopin <minutes>"
        self.tg.on("help", help_text)
        self.tg.on("start", help_text)

        actions = {"pause": self.do_pause, "resume": self.do_resume, "kill": self.do_kill,
                   "stop": lambda: (stop_requested.set(), "stopping")[1]}
        app = build_app(self.state, actions)
        server = uvicorn.Server(uvicorn.Config(app, host=self.s.dashboard_host,
                                               port=self.s.dashboard_port, log_level="warning"))

        stale = await asyncio.to_thread(self.ex.startup_cleanup)
        if stale:
            self.db.event("info", f"cancelled {stale} order(s) left from the previous run")
        await self.tg.send(f"🤖 polybot started in {self.s.mode.upper()} mode.\n" + self.summary())
        tasks = [
            self.tg.poll_forever(),
            server.serve(),  # stopped via should_exit on shutdown
            self.daily_summary(),
            self.run_every("core", self.s.mm_refresh_seconds, self.core_tick),
        ]
        if self.fc:
            tasks.append(self.run_every("forecaster", self.s.fc_poll_seconds, self.fc.step))
        if self.s.live:
            tasks.append(self.run_every("reconcile", 600, self.reconcile))
        async def timer():
            while True:
                await asyncio.sleep(5)
                if self._stop_at and time.time() >= self._stop_at:
                    log.info("timer: time is up, stopping")
                    stop_requested.set()
                    return
        tasks.append(timer())
        if self._stop_at:
            await self.tg.send(f"⏱ Will stop by itself in {self.s.run_for_minutes:g} min. "
                               "Change it any time with /stopin <minutes>.")
        running = asyncio.gather(*tasks)
        try:
            # Run until /stop (Telegram or dashboard) or Ctrl+C.
            await asyncio.wait([running, asyncio.ensure_future(stop_requested.wait())],
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            server.should_exit = True  # let the dashboard close itself instead of being cancelled
            await asyncio.sleep(0.5)
            running.cancel()
            try:
                await running
            except BaseException:  # the loops' CancelledError, expected here
                pass
            # Don't leave quotes, or shares, sitting with nobody watching them.
            message = await asyncio.to_thread(self.shutdown)
            try:
                await asyncio.wait_for(self.tg.send(message), timeout=10)
            except Exception:
                pass


def main() -> None:
    s = Settings()
    setup_logging(s.log_level)
    geo = check_geoblock()
    log.info("geoblock check: %s", geo)
    if s.live and geo.get("blocked") is not False:
        log.error("Polymarket's location check says blocked or couldn't be completed. Refusing to trade live.")
        sys.exit(1)
    if s.live:
        log.warning("LIVE MODE: real money. Exposure cap $%.2f, daily loss limit $%.2f",
                    s.max_total_exposure_usd, s.daily_loss_limit_usd)
    try:
        asyncio.run(Bot(s).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
