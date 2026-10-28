"""News-driven forecaster: new headline -> keyword prefilter -> cheap Claude screen ->
Claude probability estimate -> buy the underpriced side if the edge is big enough ->
sell when the price reaches our estimate."""

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

from polymarket import Market

from .analyst import Analyst
from .config import Settings
from .db import DB
from .exchange import BaseExchange
from .news import NewsItem, RSSSource, XSource
from .risk import Risk

log = logging.getLogger(__name__)
STRATEGY = "fc"
CONF_RANK = {"low": 0, "medium": 1, "high": 2}

STOP = set("""will what when where which with from that this have been after before about over
under into than then they their there these those would could should said says amid more most
2024 2025 2026 2027 2028 market price news live update new year week month day""".split())


def keywords(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'\-]{2,}", text)
    return {w.lower().strip("'") for w in words if w.lower() not in STOP and len(w) >= 4}


class Forecaster:
    def __init__(self, settings: Settings, db: DB, ex: BaseExchange, risk: Risk, analyst: Analyst,
                 rss: RSSSource, x: XSource | None, notify):
        self.s = settings
        self.db = db
        self.ex = ex
        self.risk = risk
        self.analyst = analyst
        self.rss = rss
        self.x = x
        self.notify = notify
        self.universe: dict[str, Market] = {}
        self.kw: dict[str, set[str]] = {}
        self.last_universe = 0.0
        self.last_analyzed: dict[str, float] = {}
        self.first_run = True

    # --- market universe -------------------------------------------------------------
    def refresh_universe(self) -> None:
        now = datetime.now(timezone.utc)
        markets: dict[str, Market] = {}
        pager = self.ex.public.list_markets(
            closed=False, volume_num_min=self.s.fc_min_volume_usd,
            end_date_min=now + timedelta(days=1), order="volume24hr", ascending=False,
            page_size=100,
        )
        for m in pager.iter_items():
            st = m.state
            if st.active and st.accepting_orders and st.enable_order_book and m.outcomes.yes.token_id:
                markets[str(m.id)] = m
            if len(markets) >= self.s.fc_universe_size:
                break
        self.universe = markets
        self.kw = {mid: keywords(f"{m.question} {m.group_item_title or ''}") for mid, m in markets.items()}
        self.last_universe = time.time()
        log.info("fc: universe refreshed, %d markets", len(markets))

    def prefilter(self, item: NewsItem, top: int = 8) -> list[str]:
        hk = keywords(f"{item.title} {item.summary[:200]}")
        scored = []
        for mid, mk in self.kw.items():
            overlap = len(hk & mk)
            if overlap >= 2:
                scored.append((overlap, mid))
        scored.sort(reverse=True)
        return [mid for _, mid in scored[:top]]

    # --- main step ---------------------------------------------------------------------
    def step(self) -> None:
        if time.time() - self.last_universe > 1800 or not self.universe:
            self.refresh_universe()
        self.manage_positions()

        items = self.rss.fetch()
        if self.x:
            items += self.x.fetch(self.s.x_poll_seconds)
        fresh = [it for it in items if self.db.mark_seen(it.id)]
        for it in fresh:
            self.db.x("INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?)",
                      (it.id, it.ts, it.source, it.title, it.summary, it.url))
        if self.first_run:
            # Don't trade on the backlog that existed before we started.
            self.first_run = False
            log.info("fc: skipped %d backlog headlines on startup", len(fresh))
            return
        if not fresh or self.risk.paused:
            return

        candidates: dict[str, list[str]] = {}
        for it in fresh:
            mids = self.prefilter(it)
            if mids:
                candidates[it.id] = mids
        if not candidates:
            return
        by_id = {it.id: it for it in fresh}
        headlines = [{"id": hid, "text": f"{by_id[hid].source}: {by_id[hid].title}"} for hid in candidates]
        market_ids = sorted({m for mids in candidates.values() for m in mids})
        markets = [{"id": mid, "question": self.universe[mid].question or ""} for mid in market_ids]
        matches = self.analyst.screen(headlines[:25], markets[:150])

        related: dict[str, list[str]] = {}
        for match in matches:
            item = by_id.get(match.headline_id)
            for mid in match.market_ids:
                if item and mid in self.universe:
                    stamp = time.strftime("%H:%M UTC", time.gmtime(item.ts))
                    related.setdefault(mid, []).append(f"[{stamp}] {item.source}: {item.title}. {item.summary[:300]}")
        for mid, news in related.items():
            self.evaluate(self.universe[mid], news)

    def evaluate(self, m: Market, news: list[str]) -> None:
        if time.time() - self.last_analyzed.get(str(m.id), 0) < 3600:
            return
        yes_tok, no_tok = str(m.outcomes.yes.token_id), str(m.outcomes.no.token_id)
        if self.db.position(yes_tok) or self.db.position(no_tok):
            return
        if len(self.db.positions(STRATEGY)) >= self.s.fc_max_open_positions:
            return
        books = self.ex.books([yes_tok, no_tok])
        yb, nb = books.get(yes_tok), books.get(no_tok)
        if not yb or not nb:
            return
        self.last_analyzed[str(m.id)] = time.time()
        self.db.set(f"q:{m.id}", m.question)
        fc = self.analyst.forecast(
            question=m.question or "", rules=m.description or "",
            end_date=str(m.state.end_date), yes_bid=yb.best_bid, yes_ask=yb.best_ask, headlines=news,
        )
        if fc is None:
            return
        p = min(max(fc.probability_yes, 0.01), 0.99)
        options = []
        if yb.best_ask is not None:
            options.append(("Yes", yes_tok, yb, p, p - yb.best_ask))
        if nb.best_ask is not None:
            options.append(("No", no_tok, nb, 1 - p, (1 - p) - nb.best_ask))
        if not options:
            return
        outcome, token, book, fair, edge = max(options, key=lambda o: o[4])
        min_edge = self.s.fc_min_edge + (0.02 if m.trading.fees_enabled else 0.0)

        action = "skip"
        why = ""
        if fc.already_priced_in:
            why = "already priced in"
        elif CONF_RANK[fc.confidence] < CONF_RANK[self.s.fc_min_confidence]:
            why = f"confidence {fc.confidence}"
        elif edge < min_edge:
            why = f"edge {edge:+.3f} < {min_edge:.2f}"
        else:
            price = book.best_ask
            kelly = edge / (1 - price)
            usd = min(self.s.fc_max_trade_usd, self.s.fc_kelly_fraction * kelly * self.ex.equity())
            shares = min(math.floor(usd / price * 100) / 100, book.ask_depth)  # round down: never exceed cap
            if shares < book.min_size:
                why = f"size {shares:.1f} below market minimum {book.min_size:g}"
            else:
                free = self.ex.cash() - self.ex.reserved_cash()
                ok, why = self.risk.allow_buy(shares * price, self.ex.exposure(), free)
                if ok:
                    oid = self.ex.take(token_id=token, side="BUY", size=shares, limit_price=price,
                                       strategy=STRATEGY, market=str(m.id), outcome=outcome,
                                       target_price=round(fair, 2))
                    action = "buy" if oid else "rejected"
                    if oid:
                        self.notify(
                            f"🧠 Bought {shares:g} {outcome} @ {price:.2f} (${shares*price:.2f})\n"
                            f"{m.question}\nClaude: {p:.0%} YES ({fc.confidence}), edge {edge:+.2f}\n"
                            f"{fc.reasoning}"
                        )
        self.db.x(
            "INSERT INTO signals (ts, market, question, prob, price, edge, confidence, reasoning, action)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), str(m.id), m.question, p, book.best_ask, edge, fc.confidence,
             fc.reasoning + (f" [{why}]" if why else ""), action),
        )
        log.info("fc: %s | p=%.2f %s edge=%+.3f -> %s %s", (m.question or "")[:60], p, outcome, edge, action, why)

    def manage_positions(self) -> None:
        """Take profit once the market price reaches our fair value estimate."""
        positions = self.db.positions(STRATEGY)
        if not positions:
            return
        resting = {o.token_id for o in self.ex.open_orders() if o.side == "SELL"}
        books = self.ex.books([p.token_id for p in positions])
        for pos in positions:
            b = books.get(pos.token_id)
            if not b or b.best_bid is None or pos.token_id in resting or pos.target_price is None:
                continue
            if b.best_bid >= pos.target_price - 0.01 and b.best_bid > pos.avg_price:
                oid = self.ex.take(token_id=pos.token_id, side="SELL", size=pos.size,
                                   limit_price=b.best_bid, strategy=STRATEGY, market=pos.market,
                                   outcome=pos.outcome)
                if oid:
                    self.notify(f"💰 Take profit: selling {pos.size:g} {pos.outcome} @ {b.best_bid:.2f} "
                                f"(bought {pos.avg_price:.2f}) → ${(b.best_bid-pos.avg_price)*pos.size:+.2f}")
