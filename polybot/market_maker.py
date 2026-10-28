"""Liquidity-rewards market maker.

Polymarket pays daily rewards to resting orders close to the midpoint in selected markets.
For each chosen market we rest a BUY on YES below the mid and a BUY on NO below (1 - mid).
If both fill we hold a YES+NO pair that is worth exactly $1, bought for less than $1, and
merge it back into cash. If only one side fills, we stop adding to it, offer it back
slightly above our cost, and cut it if the price moves too far against us.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import DB
from .exchange import BaseExchange, Book, round_to_tick
from .rewards import expected_daily_reward, others_q, our_q
from .risk import Risk

log = logging.getLogger(__name__)
STRATEGY = "mm"


@dataclass
class MMMarket:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    max_spread: float  # reward band half-width, in price units (0.03 = 3c)
    min_size: float  # shares needed for an order to earn rewards
    daily_rate: float
    score: float
    last_mid: float | None = None
    cooldown_until: float = 0.0
    tick: float = 0.01
    extra: dict = field(default_factory=dict)


@dataclass
class Quote:
    token_id: str
    side: str
    price: float
    size: float
    outcome: str


class MarketMaker:
    def __init__(self, settings: Settings, db: DB, ex: BaseExchange, risk: Risk, notify):
        self.s = settings
        self.db = db
        self.ex = ex
        self.risk = risk
        self.notify = notify
        self.markets: dict[str, MMMarket] = {}
        self.last_select = 0.0

    # --- market selection -----------------------------------------------------------
    def select_markets(self) -> None:
        affordable = [
            r for r in self.ex.public.list_current_rewards().iter_items()
            if r.rewards_min_size and r.rewards_max_spread
            # Both sides of a reward-eligible quote cost about min_size dollars together.
            and float(r.rewards_min_size) <= self.s.mm_max_per_market_usd
        ]
        # Only look up the best-paying ones; fetching every reward market takes minutes.
        affordable.sort(key=lambda r: float(r.total_daily_rate or r.native_daily_rate or 0), reverse=True)
        by_cid = {str(r.condition_id): r for r in affordable[:80]}
        if not by_cid:
            log.warning("mm: no affordable reward markets found")
            return

        now = datetime.now(timezone.utc)
        candidates: list[MMMarket] = []
        cids = list(by_cid)
        for i in range(0, len(cids), 20):
            for m in self.ex.public.list_markets(condition_ids=cids[i : i + 20], closed=False).iter_items():
                st, tr = m.state, m.trading
                if not (st.active and st.accepting_orders and st.enable_order_book) or st.closed:
                    continue
                if st.end_date is None or st.end_date - now < timedelta(days=self.s.mm_min_days_to_end):
                    continue
                if tr.seconds_delay:  # live sports markets delay orders; we'd get picked off
                    continue
                yes, no = m.outcomes.yes, m.outcomes.no
                if not yes.token_id or not no.token_id:
                    continue
                q = (m.question or "").lower()
                if any(k in q for k in self.s.mm_avoid_list):
                    continue
                r = by_cid.get(str(m.condition_id))
                if r is None:
                    continue
                rate = float(r.total_daily_rate or r.native_daily_rate or 0)
                candidates.append(MMMarket(
                    condition_id=str(m.condition_id), question=m.question or m.slug or "",
                    yes_token=str(yes.token_id), no_token=str(no.token_id),
                    max_spread=float(r.rewards_max_spread) / 100.0,
                    min_size=float(r.rewards_min_size), daily_rate=rate,
                    score=0.0,  # filled in below from the order book
                    tick=float(tr.minimum_tick_size or 0.01),
                ))

        # A wide or empty book means a jumpy midpoint and quotes that only get hit when
        # someone knows more than us. Require a real two-sided book inside the reward band.
        books = self.ex.books([c.yes_token for c in candidates]) if candidates else {}
        lo, hi = self.s.mm_price_band
        tight = []
        for c in candidates:
            b = books.get(c.yes_token)
            if not b:
                continue
            bid, ask = b.adjusted(c.min_size)
            if bid is None or ask is None or ask - bid > c.max_spread:
                continue
            mid = (bid + ask) / 2
            if not lo <= mid <= hi:
                continue
            # Rank by what we'd expect to earn per dollar, given the competition near the mid.
            offset = max(c.tick, c.max_spread * self.s.mm_spread_fraction)
            ours = our_q(mid, mid - offset, (1 - mid) - offset, c.min_size, c.max_spread)
            est = expected_daily_reward(c.daily_rate, ours, others_q(b, mid, c.max_spread, c.min_size))
            c.extra["est_daily"] = est
            c.score = est / c.min_size
            tight.append(c)
        candidates = tight
        candidates.sort(key=lambda c: c.score, reverse=True)
        # Take as many markets as the exposure cap can actually fund.
        budget = self.s.max_total_exposure_usd - sum(p.cost for p in self.db.positions())
        chosen: dict[str, MMMarket] = {}
        for c in candidates:
            if len(chosen) >= self.s.mm_max_markets or c.min_size > budget:
                continue
            chosen[c.condition_id] = c
            budget -= c.min_size
        # Keep markets we still hold inventory in, even if they dropped out of the top list.
        held = {p.market for p in self.db.positions(STRATEGY)}
        for cid, old in self.markets.items():
            if cid in held and cid not in chosen:
                chosen[cid] = old
        # Cancel every MM order outside the chosen markets, including ones left over from a
        # previous run that this process never knew about.
        keep_tokens = {t for c in chosen.values() for t in (c.yes_token, c.no_token)}
        for o in self.ex.open_orders():
            if o.strategy == STRATEGY and o.token_id not in keep_tokens:
                self.ex.cancel(o.order_id)
        new = set(chosen) - set(self.markets)
        self.markets = {cid: self.markets.get(cid, c) for cid, c in chosen.items()}
        self.last_select = time.time()
        for cid, m in self.markets.items():
            self.db.set(f"q:{cid}", m.question)
        for cid in new:
            m = self.markets[cid]
            self.db.event("info", f"mm: quoting '{m.question}' (${m.daily_rate:.0f}/day pool, "
                                  f"our est. ${m.extra.get('est_daily', 0):.2f}/day, "
                                  f"min {m.min_size:g} sh, band {m.max_spread*100:.1f}c)")

    # --- quoting --------------------------------------------------------------------------
    def _cancel_market(self, m: MMMarket) -> None:
        for o in self.ex.open_orders():
            if o.strategy == STRATEGY and o.token_id in (m.yes_token, m.no_token):
                self.ex.cancel(o.order_id)

    def desired_quotes(self, m: MMMarket, yes: Book, no: Book) -> list[Quote]:
        mid = yes.adjusted_mid(m.min_size)
        if mid is None:
            return []
        tick = max(yes.tick, m.tick)
        offset = max(tick, m.max_spread * self.s.mm_spread_fraction)
        size = max(m.min_size, yes.min_size, no.min_size)
        pos_yes = self.db.position(m.yes_token)
        pos_no = self.db.position(m.no_token)
        quotes: list[Quote] = []

        bid_yes = round_to_tick(mid - offset, tick, "BUY")
        bid_no = round_to_tick((1 - mid) - offset, tick, "BUY")

        if pos_yes and not pos_no:
            # Holding YES: offer it back, and only buy NO cheap enough to lock a profit.
            quotes.append(Quote(m.yes_token, "SELL",
                                round_to_tick(max(pos_yes.avg_price + tick, mid + offset), tick, "SELL"),
                                pos_yes.size, "Yes"))
            bid_no = min(bid_no, round_to_tick(1 - pos_yes.avg_price - tick, tick, "BUY"))
            quotes.append(Quote(m.no_token, "BUY", bid_no, size, "No"))
        elif pos_no and not pos_yes:
            quotes.append(Quote(m.no_token, "SELL",
                                round_to_tick(max(pos_no.avg_price + tick, (1 - mid) + offset), tick, "SELL"),
                                pos_no.size, "No"))
            bid_yes = min(bid_yes, round_to_tick(1 - pos_no.avg_price - tick, tick, "BUY"))
            quotes.append(Quote(m.yes_token, "BUY", bid_yes, size, "Yes"))
        elif not pos_yes and not pos_no:
            quotes.append(Quote(m.yes_token, "BUY", bid_yes, size, "Yes"))
            quotes.append(Quote(m.no_token, "BUY", bid_no, size, "No"))

        # Never cross the book: a post-only order that would trade gets rejected anyway.
        out = []
        for q in quotes:
            book = yes if q.token_id == m.yes_token else no
            if q.side == "BUY" and book.best_ask is not None and q.price >= book.best_ask:
                q.price = round_to_tick(book.best_ask - tick, tick, "BUY")
            if q.side == "SELL" and book.best_bid is not None and q.price <= book.best_bid:
                q.price = round_to_tick(book.best_bid + tick, tick, "SELL")
            if 0 < q.price < 1 and q.size > 0:
                out.append(q)
        return out

    def _stop_loss(self, m: MMMarket, yes: Book, no: Book) -> bool:
        for token, book, outcome in ((m.yes_token, yes, "Yes"), (m.no_token, no, "No")):
            pos = self.db.position(token)
            other = self.db.position(m.no_token if token == m.yes_token else m.yes_token)
            if not pos or other or book.best_bid is None:
                continue
            if book.best_bid <= pos.avg_price - self.s.mm_stop_loss:
                self._cancel_market(m)
                self.ex.take(token_id=token, side="SELL", size=pos.size, limit_price=book.best_bid,
                             strategy=STRATEGY, market=m.condition_id, outcome=outcome)
                self.notify(f"🛑 MM stop-loss: sold {pos.size:g} {outcome} in '{m.question}' "
                            f"at {book.best_bid:.2f} (cost {pos.avg_price:.2f})")
                m.cooldown_until = time.time() + self.s.mm_cooldown_seconds
                return True
        return False

    def _merge_pairs(self, m: MMMarket) -> None:
        pos_yes, pos_no = self.db.position(m.yes_token), self.db.position(m.no_token)
        if not pos_yes or not pos_no:
            return
        amount = min(pos_yes.size, pos_no.size)
        if amount < 1:
            return
        profit = amount * (1 - pos_yes.avg_price - pos_no.avg_price)
        if self.ex.merge(m.condition_id, amount, pos_yes, pos_no):
            self.notify(f"✅ MM pair completed in '{m.question}': merged {amount:g} pairs, "
                        f"locked ${profit:+.2f}")

    def step(self) -> None:
        if time.time() - self.last_select > 1800 or not self.markets:
            self.select_markets()
        if not self.markets:
            return
        tokens = [t for m in self.markets.values() for t in (m.yes_token, m.no_token)]
        books = self.ex.books(tokens)
        open_orders = [o for o in self.ex.open_orders() if o.strategy == STRATEGY]

        for m in list(self.markets.values()):
            yes, no = books.get(m.yes_token), books.get(m.no_token)
            if not yes or not no:
                continue
            self._merge_pairs(m)
            if self._stop_loss(m, yes, no):
                continue
            mid = yes.adjusted_mid(m.min_size)
            if mid is None:  # one side of the book emptied out: don't quote into nothing
                self._cancel_market(m)
                continue
            # Big jump = news. Get out of the way rather than be the liquidity informed traders hit.
            if m.last_mid is not None and abs(mid - m.last_mid) >= self.s.mm_jump_threshold:
                self._cancel_market(m)
                m.cooldown_until = time.time() + self.s.mm_cooldown_seconds
                jumps = [t for t in m.extra.get("jumps", []) if time.time() - t < 3600] + [time.time()]
                m.extra["jumps"] = jumps
                if len(jumps) >= 3:  # too jumpy to quote safely: swap it for another market
                    del self.markets[m.condition_id]
                    self.last_select = 0.0
                    self.db.event("warn", f"mm: dropping '{m.question}', moved 3 times in an hour")
                    continue
                self.db.event("warn", f"mm: '{m.question}' moved {m.last_mid:.3f}->{mid:.3f}, cooling down")
            m.last_mid = mid
            if time.time() < m.cooldown_until or self.risk.paused:
                self._cancel_market(m)
                continue

            want = self.desired_quotes(m, yes, no)
            mine = [o for o in open_orders if o.token_id in (m.yes_token, m.no_token)]
            keep = set()
            for q in want:
                match = next((o for o in mine if o.token_id == q.token_id and o.side == q.side
                              and abs(o.price - q.price) < 1e-9 and abs(o.size - q.size) < 0.5
                              and (o.expires_at is None or o.expires_at - time.time() > 120)), None)
                if match:  # unchanged quote: leave it, keep queue priority
                    keep.add(match.order_id)
                    continue
                if q.side == "BUY":
                    # The order being replaced frees its own cash, so don't count it twice.
                    replaced = sum(o.price * o.size for o in mine
                                   if o.token_id == q.token_id and o.side == "BUY")
                    free = self.ex.cash() - self.ex.reserved_cash() + replaced
                    ok, why = self.risk.allow_buy(q.price * q.size, self.ex.exposure() - replaced, free)
                    if not ok:
                        log.debug("mm: skip %s %s: %s", q.outcome, m.question[:40], why)
                        continue
                self._replace(mine, q, m)
            for o in mine:
                if o.order_id not in keep and not any(
                    o.token_id == q.token_id and o.side == q.side for q in want
                ):
                    self.ex.cancel(o.order_id)
            self._accrue_rewards(m, yes, mid)

    def _accrue_rewards(self, m: MMMarket, yes: Book, mid: float) -> None:
        """Estimate what our resting quotes earned since the last step."""
        now = time.time()
        last = m.extra.get("last_accrual", now)
        m.extra["last_accrual"] = now
        dt = min(now - last, 3 * self.s.mm_refresh_seconds)  # ignore gaps (sleep, restarts)
        if dt <= 0:
            return
        resting = [o for o in self.ex.open_orders() if o.strategy == STRATEGY and o.side == "BUY"
                   and o.token_id in (m.yes_token, m.no_token) and o.size >= m.min_size]
        yes_bid = max((o.price for o in resting if o.token_id == m.yes_token), default=None)
        no_bid = max((o.price for o in resting if o.token_id == m.no_token), default=None)
        ours = our_q(mid, yes_bid, no_bid, m.min_size, m.max_spread)
        if ours <= 0:
            return
        daily = expected_daily_reward(m.daily_rate, ours, others_q(yes, mid, m.max_spread, m.min_size))
        self.db.add_reward("est", daily * dt / 86400)

    def _replace(self, mine, q: Quote, m: MMMarket) -> None:
        for o in mine:
            if o.token_id == q.token_id and o.side == q.side:
                self.ex.cancel(o.order_id)
        self.ex.place_limit(token_id=q.token_id, side=q.side, price=q.price, size=q.size,
                            strategy=STRATEGY, market=m.condition_id, outcome=q.outcome,
                            post_only=True)
