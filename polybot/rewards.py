"""Polymarket liquidity-reward scoring, following the published formula:

    S(s) = ((v - s) / v)^2 * size     for an order s away from the adjusted midpoint, v = max spread

Q_one sums scores of orders on one side of the market (YES bids, i.e. NO asks) and Q_two
the other (YES asks / NO bids). Two-sided makers keep min(Q_one, Q_two); one-sided quoting
counts for a third (only while the midpoint is between 0.10 and 0.90). A maker's share of
the market's daily pool is their Q_min over everyone's.

We only see aggregate depth, not individual makers, so other makers are treated as one
combined maker. That makes these numbers estimates for ranking markets and judging paper
results; live mode reads the real payouts from Polymarket instead.
"""

from .exchange import Book

SINGLE_SIDED_DIVISOR = 3.0


def order_score(distance: float, size: float, max_spread: float) -> float:
    if distance < 0 or distance >= max_spread:
        return 0.0
    return ((max_spread - distance) / max_spread) ** 2 * size


def q_min(q_one: float, q_two: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / SINGLE_SIDED_DIVISOR)
    return min(q_one, q_two)


def others_q(yes_book: Book, mid: float, max_spread: float, min_size: float) -> float:
    """Combined score of resting orders in the YES book (the NO book mirrors it)."""
    q_one = sum(order_score(mid - p, sz, max_spread) for p, sz in yes_book.bids if sz >= min_size)
    q_two = sum(order_score(p - mid, sz, max_spread) for p, sz in yes_book.asks if sz >= min_size)
    return q_min(q_one, q_two, mid)


def our_q(mid: float, yes_bid: float | None, no_bid: float | None, size: float, max_spread: float) -> float:
    q_one = order_score(mid - yes_bid, size, max_spread) if yes_bid is not None else 0.0
    # A NO bid at p sits on the YES ask side at 1 - p.
    q_two = order_score((1 - no_bid) - mid, size, max_spread) if no_bid is not None else 0.0
    return q_min(q_one, q_two, mid)


def expected_daily_reward(daily_rate: float, ours: float, others: float) -> float:
    if ours <= 0:
        return 0.0
    return daily_rate * ours / (ours + others)
