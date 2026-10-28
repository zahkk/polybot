"""Exchange layer. PaperExchange and LiveExchange share one interface so strategies
never know (or care) whether money is real.

Both read real market data from Polymarket. Paper mode simulates fills: a resting BUY
fills when the best ask drops to our price, a SELL when the best bid rises to it.
That ignores queue position, so paper results are optimistic for market making.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from polymarket import AcceptedOrder, InsufficientLiquidityError, PublicClient, SecureClient

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


@dataclass
class Book:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    tick: float
    min_size: float
    bid_depth: float  # shares at best bid
    ask_depth: float
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size), best first
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def adjusted(self, min_size: float) -> tuple[float | None, float | None]:
        """Best bid/ask ignoring dust orders smaller than min_size. Polymarket scores rewards
        against this "adjusted midpoint", and it doesn't jump around when someone posts 1 share."""
        bid = next((p for p, sz in self.bids if sz >= min_size), None)
        ask = next((p for p, sz in self.asks if sz >= min_size), None)
        return bid, ask

    def adjusted_mid(self, min_size: float) -> float | None:
        bid, ask = self.adjusted(min_size)
        return None if bid is None or ask is None else (bid + ask) / 2


@dataclass
class Order:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float  # remaining
    strategy: str
    market: str
    outcome: str
    expires_at: float | None = None  # unix time; live quotes expire so a dead bot leaves nothing behind


class _Locked:
    """Serialize calls into an SDK client. The dashboard, Telegram and trading loops run in
    different threads, and sharing one HTTP/2 connection between them fails on Windows
    (WinError 10035)."""

    def __init__(self, target):
        self._target = target
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)

        return call


def _to_book(ob) -> Book:
    # The CLOB returns bids ascending and asks descending, so the best level is last.
    bids = sorted(ob.bids, key=lambda lv: lv.price)
    asks = sorted(ob.asks, key=lambda lv: lv.price, reverse=True)
    return Book(
        token_id=str(ob.asset_id),
        best_bid=float(bids[-1].price) if bids else None,
        best_ask=float(asks[-1].price) if asks else None,
        tick=float(ob.tick_size),
        min_size=float(ob.min_order_size),
        bid_depth=float(bids[-1].size) if bids else 0.0,
        ask_depth=float(asks[-1].size) if asks else 0.0,
        bids=[(float(lv.price), float(lv.size)) for lv in reversed(bids)],
        asks=[(float(lv.price), float(lv.size)) for lv in reversed(asks)],
    )


def round_to_tick(price: float, tick: float, side: str) -> float:
    """Round down for buys and up for sells so we never pay more than intended."""
    p, t = Decimal(str(price)), Decimal(str(tick))
    steps = (p / t).to_integral_value(rounding=ROUND_FLOOR if side == "BUY" else ROUND_CEILING)
    return float(min(max(steps * t, t), 1 - t))


class BaseExchange:
    def __init__(self, settings: Settings, db: DB, public: PublicClient):
        self.s = settings
        self.db = db
        self.public = public

    # --- market data (shared) --------------------------------------------------------
    def book(self, token_id: str) -> Book:
        return _to_book(self.public.get_order_book(token_id=token_id))

    def books(self, token_ids: list[str]) -> dict[str, Book]:
        out: dict[str, Book] = {}
        for i in range(0, len(token_ids), 50):
            for ob in self.public.get_order_books(token_ids=token_ids[i : i + 50]):
                b = _to_book(ob)
                out[b.token_id] = b
        return out

    def midpoints(self, token_ids: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(token_ids), 50):
            for k, v in self.public.get_midpoints(token_ids=token_ids[i : i + 50]).items():
                out[str(k)] = float(v)
        return out

    # --- accounting (shared) -----------------------------------------------------------
    def equity(self) -> float:
        positions = self.db.positions()
        mids = self.midpoints([p.token_id for p in positions]) if positions else {}
        value = sum(p.size * mids.get(p.token_id, p.avg_price) for p in positions)
        # Resting orders don't move cash (paper or on-chain) until they fill, so cash + positions
        # is the whole account. Adding reserved cash here would double count it.
        return self.cash() + value

    def exposure(self) -> float:
        """Cost basis of positions plus cash locked in resting buy orders."""
        return sum(p.cost for p in self.db.positions()) + self.reserved_cash()

    def reserved_cash(self) -> float:
        return sum(o.price * o.size for o in self.open_orders() if o.side == "BUY")

    # --- interface ------------------------------------------------------------------------
    def cash(self) -> float: ...
    def open_orders(self, token_id: str | None = None) -> list[Order]: ...
    def place_limit(self, *, token_id: str, side: str, price: float, size: float,
                    strategy: str, market: str, outcome: str, post_only: bool = False,
                    target_price: float | None = None) -> str | None: ...
    def cancel(self, order_id: str) -> None: ...
    def cancel_all(self) -> None: ...
    def sync_fills(self) -> list[dict]: ...
    def merge(self, condition_id: str, amount: float, pos_yes, pos_no) -> bool: ...
    def take(self, *, token_id: str, side: str, size: float, limit_price: float, strategy: str,
             market: str, outcome: str, target_price: float | None = None) -> str | None:
        """Fill-and-kill: trade what's available now at limit_price or better, cancel the rest.
        Nothing is ever left resting on the book."""
        ...
    def startup_cleanup(self) -> int:
        """Called once at start: deal with orders left by a previous run. Returns how many."""
        ...

    def _record_merge(self, amount: float, pos_yes, pos_no) -> None:
        # A YES+NO pair redeems for $1. Book the whole profit on the YES leg.
        self.db.record_fill(strategy=pos_yes.strategy, token_id=pos_yes.token_id, market=pos_yes.market,
                            outcome=pos_yes.outcome, side="SELL", price=1 - pos_no.avg_price,
                            size=amount, order_id="merge")
        self.db.record_fill(strategy=pos_no.strategy, token_id=pos_no.token_id, market=pos_no.market,
                            outcome=pos_no.outcome, side="SELL", price=pos_no.avg_price,
                            size=amount, order_id="merge")


class PaperExchange(BaseExchange):
    def __init__(self, settings: Settings, db: DB, public: PublicClient):
        super().__init__(settings, db, public)
        self._pending_fills: list[dict] = []
        self._lock = threading.RLock()  # MM and forecaster threads share the paper wallet
        if db.get("paper_cash") is None:
            db.set("paper_cash", settings.paper_starting_cash)

    def cash(self) -> float:
        return float(self.db.get("paper_cash"))

    def _adjust_cash(self, delta: float) -> None:
        self.db.set("paper_cash", self.cash() + delta)

    def open_orders(self, token_id: str | None = None) -> list[Order]:
        sql, args = "SELECT * FROM paper_orders", ()
        if token_id:
            sql, args = sql + " WHERE token_id=?", (token_id,)
        return [
            Order(r["order_id"], r["token_id"], r["side"], r["price"], r["size"], r["strategy"],
                  r["market"], (self.db.get(f"paper_meta:{r['order_id']}") or {}).get("outcome", ""))
            for r in self.db.q(sql, args)
        ]

    def place_limit(self, *, token_id, side, price, size, strategy, market, outcome,
                    post_only=False, target_price=None):
        book = self.book(token_id)
        crosses = (side == "BUY" and book.best_ask is not None and price >= book.best_ask) or (
            side == "SELL" and book.best_bid is not None and price <= book.best_bid
        )
        if crosses and post_only:
            log.info("paper: post-only order would cross, rejected")
            return None
        if side == "BUY" and price * size > self.cash() - self.reserved_cash() + 1e-9:
            log.warning("paper: not enough cash for order")
            return None
        if side == "SELL":
            pos = self.db.position(token_id)
            if not pos or pos.size + 1e-9 < size:
                log.warning("paper: not enough shares to sell")
                return None
        oid = "paper-" + uuid.uuid4().hex[:12]
        if crosses:
            # Taker: fill immediately at the touch (top-of-book only, size permitting).
            fill_px = book.best_ask if side == "BUY" else book.best_bid
            self._fill(oid, token_id, side, fill_px, size, strategy, market, outcome, target_price)
            return oid
        self.db.x(
            "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, time.time(), strategy, token_id, market, side, price, size),
        )
        self.db.set(f"paper_meta:{oid}", {"outcome": outcome, "target": target_price})
        return oid

    def _fill(self, oid, token_id, side, price, size, strategy, market, outcome, target):
        self._adjust_cash(-price * size if side == "BUY" else price * size)
        self.db.record_fill(strategy=strategy, token_id=token_id, market=market, outcome=outcome,
                            side=side, price=price, size=size, order_id=oid, target_price=target)
        self._pending_fills.append({"order_id": oid, "token_id": token_id, "side": side,
                                    "price": price, "size": size, "strategy": strategy,
                                    "market": market, "outcome": outcome})

    def take(self, *, token_id, side, size, limit_price, strategy, market, outcome, target_price=None):
        book = self.book(token_id)
        if side == "BUY":
            px, depth = book.best_ask, book.ask_depth
            ok = px is not None and px <= limit_price + 1e-9
        else:
            px, depth = book.best_bid, book.bid_depth
            ok = px is not None and px >= limit_price - 1e-9
            pos = self.db.position(token_id)
            size = min(size, pos.size if pos else 0.0)
        size = min(size, depth)
        if not ok or size <= 0:
            return None
        if side == "BUY" and px * size > self.cash() - self.reserved_cash() + 1e-9:
            return None
        oid = "paper-" + uuid.uuid4().hex[:12]
        self._fill(oid, token_id, side, px, size, strategy, market, outcome, target_price)
        return oid

    def merge(self, condition_id, amount, pos_yes, pos_no) -> bool:
        self._adjust_cash(amount)
        self._record_merge(amount, pos_yes, pos_no)
        return True

    def cancel(self, order_id: str) -> None:
        self.db.x("DELETE FROM paper_orders WHERE order_id=?", (order_id,))

    def cancel_all(self) -> None:
        self.db.x("DELETE FROM paper_orders")

    def startup_cleanup(self) -> int:
        # Nobody watched these while the bot was off; filling them against today's prices
        # would invent trades. Drop them and quote fresh.
        n = len(self.db.q("SELECT order_id FROM paper_orders"))
        self.cancel_all()
        return n

    def sync_fills(self) -> list[dict]:
        rows = self.db.q("SELECT * FROM paper_orders")
        if rows:
            books = self.books(list({r["token_id"] for r in rows}))
            for r in rows:
                b = books.get(r["token_id"])
                if not b:
                    continue
                hit = (r["side"] == "BUY" and b.best_ask is not None and b.best_ask <= r["price"]) or (
                    r["side"] == "SELL" and b.best_bid is not None and b.best_bid >= r["price"]
                )
                if hit:
                    meta = self.db.get(f"paper_meta:{r['order_id']}", {}) or {}
                    self.cancel(r["order_id"])
                    self._fill(r["order_id"], r["token_id"], r["side"], r["price"], r["size"],
                               r["strategy"], r["market"], meta.get("outcome", ""), meta.get("target"))
        fills, self._pending_fills = self._pending_fills, []
        return fills


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


for _name in ("place_limit", "take", "merge", "sync_fills", "cancel", "cancel_all"):
    setattr(PaperExchange, _name, _locked(getattr(PaperExchange, _name)))


class LiveExchange(BaseExchange):
    """Real orders through Polymarket's official SDK. Our order metadata (which strategy,
    market, outcome) is kept in the kv table keyed by order id so fills can be attributed."""

    def __init__(self, settings: Settings, db: DB, public: PublicClient):
        super().__init__(settings, db, public)
        self.client = _Locked(SecureClient.create(
            private_key=settings.poly_private_key,
            wallet=settings.poly_wallet_address or None,
            api_key=settings.builder_api_key,  # gasless approvals and merges
        ))
        log.info("live: trading as wallet %s (signer %s)", self.client.wallet, self.client.signer)
        if self.client.get_closed_only_mode():
            raise RuntimeError("Polymarket says this account is in close-only mode; refusing to trade.")
        self._fills: list[dict] = []

    def cash(self) -> float:
        bal = self.client.get_balance_allowance(asset_type="COLLATERAL")
        return bal.balance / 1e6  # collateral has 6 decimals

    def _tracked(self) -> dict:
        return self.db.get("live_orders", {}) or {}

    def _save_tracked(self, d: dict) -> None:
        self.db.set("live_orders", d)

    def open_orders(self, token_id: str | None = None) -> list[Order]:
        tracked = self._tracked()
        out = []
        for o in self.client.list_open_orders(token_id=token_id).iter_items():
            meta = tracked.get(o.id, {})
            out.append(Order(o.id, str(o.asset_id), o.side, float(o.price),
                             float(o.original_size - o.size_matched),
                             meta.get("strategy", "manual"), meta.get("market", str(o.condition_id)),
                             meta.get("outcome", o.outcome),
                             o.expires_at.timestamp() if o.expires_at else None))
        return out

    def place_limit(self, *, token_id, side, price, size, strategy, market, outcome,
                    post_only=False, target_price=None):
        # Resting quotes expire on their own; the market maker renews them well before that.
        # If the laptop sleeps or the bot crashes, nothing is left on the book for long.
        expiration = int(time.time()) + self.s.order_ttl_seconds if post_only else None
        resp = self.client.place_limit_order(
            token_id=token_id, price=Decimal(str(price)), size=Decimal(str(round(size, 2))),
            side=side, post_only=post_only, expiration=expiration,
        )
        if not isinstance(resp, AcceptedOrder):
            log.warning("live: order rejected: %s %s", resp.code, resp.message)
            self.db.event("warn", f"order rejected: {resp.code} {resp.message}")
            return None
        tracked = self._tracked()
        tracked[resp.order_id] = {"strategy": strategy, "market": market, "outcome": outcome,
                                  "token_id": token_id, "side": side, "price": price,
                                  "matched": 0.0, "target": target_price}
        self._save_tracked(tracked)
        return resp.order_id

    def take(self, *, token_id, side, size, limit_price, strategy, market, outcome, target_price=None):
        try:
            if side == "BUY":
                resp = self.client.place_market_order(
                    token_id=token_id, side="BUY", amount=Decimal(str(round(size * limit_price, 2))),
                    max_price=Decimal(str(limit_price)), order_type="FAK")
            else:
                resp = self.client.place_market_order(
                    token_id=token_id, side="SELL", shares=Decimal(str(round(size, 2))),
                    min_price=Decimal(str(limit_price)), order_type="FAK")
        except InsufficientLiquidityError:
            log.info("live: nothing to take at %s on %s", limit_price, token_id)
            return None
        if not isinstance(resp, AcceptedOrder):
            log.warning("live: FAK rejected: %s %s", resp.code, resp.message)
            self.db.event("warn", f"order rejected: {resp.code} {resp.message}")
            return None
        # Track it like any order so sync_fills books exactly what matched.
        tracked = self._tracked()
        tracked[resp.order_id] = {"strategy": strategy, "market": market, "outcome": outcome,
                                  "token_id": token_id, "side": side, "price": limit_price,
                                  "matched": 0.0, "target": target_price}
        self._save_tracked(tracked)
        return resp.order_id

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id=order_id)

    def cancel_all(self) -> None:
        """Cancel the bot's orders only; anything placed by hand on polymarket.com stays."""
        ids = [o.order_id for o in self.open_orders() if o.strategy != "manual"]
        if ids:
            self.client.cancel_orders(order_ids=ids)

    def startup_cleanup(self) -> int:
        # Record anything that really filled while we were away, then start from a clean book.
        self.sync_fills()
        n = len([o for o in self.open_orders() if o.strategy != "manual"])
        self.cancel_all()
        return n

    def merge(self, condition_id, amount, pos_yes, pos_no) -> bool:
        try:
            # Gasless through Polymarket's relayer; amount is in 6-decimal base units.
            self.client.merge_positions(condition_id=condition_id, amount=int(amount * 1e6)).wait()
        except Exception as e:
            log.warning("live: merge failed for %s: %s", condition_id, e)
            self.db.event("warn", f"merge failed: {e}")
            return False
        self._record_merge(amount, pos_yes, pos_no)
        return True


    def sync_fills(self) -> list[dict]:
        """Diff each tracked order's matched size against what we last saw."""
        tracked = self._tracked()
        if not tracked:
            return []
        open_ids = {o.id: o for o in self.client.list_open_orders().iter_items()}
        fills = []
        for oid, meta in list(tracked.items()):
            o = open_ids.get(oid)
            if o is None:
                try:
                    o = self.client.get_order(order_id=oid)
                except Exception as e:  # order history unavailable: stop tracking it
                    log.warning("live: could not read final state of %s: %s", oid, e)
                    del tracked[oid]
                    continue
            matched = float(o.size_matched)
            delta = matched - meta["matched"]
            if delta > 1e-9:
                self.db.record_fill(strategy=meta["strategy"], token_id=meta["token_id"],
                                    market=meta["market"], outcome=meta["outcome"],
                                    side=meta["side"], price=float(o.price), size=delta,
                                    order_id=oid, target_price=meta.get("target"))
                fills.append({"order_id": oid, "token_id": meta["token_id"], "side": meta["side"],
                              "price": float(o.price), "size": delta, "strategy": meta["strategy"],
                              "market": meta["market"], "outcome": meta["outcome"]})
                meta["matched"] = matched
            if oid not in open_ids:
                del tracked[oid]
        self._save_tracked(tracked)
        return fills

    def rewards_paid(self, day: str) -> float:
        """Liquidity rewards Polymarket credited us for a UTC day (YYYY-MM-DD)."""
        return sum(float(e.earnings) for e in self.client.get_total_earnings_for_user_for_day(date=day))

    def reconcile_positions(self) -> None:
        """Overwrite local share counts with what Polymarket's data API reports."""
        actual = {str(p.asset_id): float(p.current_size)
                  for p in self.client.list_positions().iter_items()}
        for p in self.db.positions():
            real = actual.get(p.token_id, 0.0)
            if abs(real - p.size) > 0.01:
                log.warning("reconcile: %s local %.2f vs actual %.2f", p.token_id, p.size, real)
                self.db.x("UPDATE positions SET size=? WHERE token_id=?", (real, p.token_id))


def make_exchange(settings: Settings, db: DB) -> BaseExchange:
    public = _Locked(PublicClient())
    if settings.live:
        if not settings.poly_private_key:
            raise RuntimeError("MODE=live needs POLY_PRIVATE_KEY")
        return LiveExchange(settings, db, public)
    return PaperExchange(settings, db, public)
