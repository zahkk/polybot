"""In-memory stand-in for Polymarket's PublicClient."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS


class Pager:
    def __init__(self, items):
        self.items = list(items)

    def iter_items(self):
        return iter(self.items)


def level(price, size):
    return NS(price=Decimal(str(price)), size=Decimal(str(size)))


class FakePublic:
    def __init__(self):
        self.books = {}  # token -> (bid, ask)
        self.markets = []
        self.rewards = []

    def set_book(self, token, bid, ask, size=100, tick=0.01, min_size=5):
        self.books[token] = (bid, ask, size, tick, min_size)

    def set_levels(self, token, bids, asks, tick=0.01, min_size=5):
        """bids/asks: [(price, size), ...] in any order."""
        self.books[token] = ("levels", bids, asks, tick, min_size)

    def _ob(self, token):
        entry = self.books[token]
        if entry[0] == "levels":
            _, bids, asks, tick, min_size = entry
            # Mimic the CLOB: bids ascending, asks descending.
            return NS(asset_id=token, bids=[level(p, s) for p, s in sorted(bids)],
                      asks=[level(p, s) for p, s in sorted(asks, reverse=True)],
                      tick_size=Decimal(str(tick)), min_order_size=Decimal(str(min_size)))
        bid, ask, size, tick, min_size = entry
        return NS(asset_id=token, bids=[level(bid, size)] if bid is not None else [],
                  asks=[level(ask, size)] if ask is not None else [],
                  tick_size=Decimal(str(tick)), min_order_size=Decimal(str(min_size)))

    def get_order_book(self, *, token_id):
        return self._ob(token_id)

    def get_order_books(self, *, token_ids):
        return tuple(self._ob(t) for t in token_ids if t in self.books)

    def get_midpoints(self, *, token_ids):
        out = {}
        for t in token_ids:
            if t in self.books:
                b = self._ob(t)
                if b.bids and b.asks:
                    out[t] = (b.bids[-1].price + b.asks[-1].price) / 2
        return out

    def list_current_rewards(self, **_):
        return Pager(self.rewards)

    def list_markets(self, condition_ids=None, **_):
        ms = self.markets
        if condition_ids:
            ms = [m for m in ms if m.condition_id in condition_ids]
        return Pager(ms)


def make_market(mid="m1", cid="c1", question="Will X happen by June?", yes="Y1", no="N1",
                price=0.5, days=30, fees=False, description="Resolves YES if X happens."):
    end = datetime.now(timezone.utc) + timedelta(days=days)
    return NS(
        id=mid, condition_id=cid, question=question, slug=None, group_item_title=None,
        description=description,
        state=NS(active=True, closed=False, accepting_orders=True, enable_order_book=True, end_date=end),
        trading=NS(seconds_delay=0, minimum_tick_size=Decimal("0.01"), fees_enabled=fees),
        outcomes=NS(yes=NS(token_id=yes, price=Decimal(str(price))),
                    no=NS(token_id=no, price=Decimal(str(1 - price)))),
        metrics=NS(liquidity_clob=Decimal("1000"), liquidity_num=Decimal("1000")),
    )


def make_reward(cid="c1", max_spread=3.0, min_size=10, rate=20):
    return NS(condition_id=cid, rewards_max_spread=max_spread, rewards_min_size=Decimal(str(min_size)),
              total_daily_rate=Decimal(str(rate)), native_daily_rate=Decimal(str(rate)))
