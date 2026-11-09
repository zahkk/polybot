import asyncio

import pytest
from fakes import FakeHandle, FakePublic, FakeStreamClient, book_event, change_event, make_market, make_reward, tick_event
from fastapi.testclient import TestClient

from polybot.analyst import Forecast
from polybot.config import Settings
from polybot.dashboard import build_app
from polybot.db import DB
from polybot.exchange import BookCache, PaperExchange, round_to_tick, run_market_stream
from polybot.forecaster import Forecaster
from polybot.market_maker import MarketMaker
from polybot.risk import Risk


@pytest.fixture
def env(tmp_path):
    s = Settings(_env_file=None, db_path=str(tmp_path / "t.db"), paper_starting_cash=100.0)
    db = DB(s.db_path)
    pub = FakePublic()
    ex = PaperExchange(s, db, pub)
    risk = Risk(s, db)
    notes = []
    return s, db, pub, ex, risk, notes


def test_round_to_tick():
    assert round_to_tick(0.4749, 0.01, "BUY") == 0.47
    assert round_to_tick(0.4701, 0.01, "SELL") == 0.48
    assert round_to_tick(0.47, 0.01, "SELL") == 0.47
    assert round_to_tick(0.0001, 0.01, "BUY") == 0.01
    assert round_to_tick(0.9999, 0.01, "SELL") == 0.99


def test_paper_resting_order_fills_when_book_crosses(env):
    s, db, pub, ex, risk, _ = env
    pub.set_book("Y1", 0.48, 0.52)
    oid = ex.place_limit(token_id="Y1", side="BUY", price=0.47, size=10, strategy="mm",
                         market="c1", outcome="Yes", post_only=True)
    assert oid and ex.sync_fills() == []
    assert ex.reserved_cash() == pytest.approx(4.7)
    pub.set_book("Y1", 0.44, 0.47)
    fills = ex.sync_fills()
    assert len(fills) == 1 and fills[0]["outcome"] == "Yes"
    assert ex.cash() == pytest.approx(95.3)
    assert db.position("Y1").size == 10
    # Taker sell fills immediately at the bid.
    ex.place_limit(token_id="Y1", side="SELL", price=0.40, size=10, strategy="mm",
                   market="c1", outcome="Yes")
    assert db.position("Y1") is None
    assert db.get("realized_pnl") == pytest.approx(-0.3)


def test_post_only_rejects_crossing_order(env):
    s, db, pub, ex, risk, _ = env
    pub.set_book("Y1", 0.48, 0.52)
    assert ex.place_limit(token_id="Y1", side="BUY", price=0.53, size=10, strategy="mm",
                          market="c1", outcome="Yes", post_only=True) is None


def test_mm_quotes_both_sides_inside_reward_band(env):
    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0, min_size=10)]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    orders = sorted(ex.open_orders(), key=lambda o: o.token_id)
    assert [(o.token_id, o.side, o.price, o.size) for o in orders] == [
        ("N1", "BUY", 0.48, 10), ("Y1", "BUY", 0.48, 10)]
    # Unchanged market -> orders are left alone (same ids, keeps queue priority).
    ids = {o.order_id for o in orders}
    mm.step()
    assert {o.order_id for o in ex.open_orders()} == ids


def test_mm_one_leg_filled_offers_it_back_and_caps_other_bid(env):
    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0, min_size=10)]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    pub.set_book("Y1", 0.47, 0.48)  # someone sells into our YES bid
    ex.sync_fills()
    pub.set_book("Y1", 0.47, 0.49)
    mm.step()
    by = {(o.token_id, o.side): o for o in ex.open_orders()}
    assert by[("Y1", "SELL")].price >= 0.49
    assert ("Y1", "BUY") not in by
    assert by[("N1", "BUY")].price <= 1 - 0.48 - 0.01


def test_mm_merges_completed_pair(env):
    s, db, pub, ex, risk, notes = env
    db.record_fill(strategy="mm", token_id="Y1", market="c1", outcome="Yes", side="BUY", price=0.48, size=10)
    db.record_fill(strategy="mm", token_id="N1", market="c1", outcome="No", side="BUY", price=0.49, size=10)
    db.set("paper_cash", 100 - 9.7)
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward()]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    assert db.position("Y1") is None and db.position("N1") is None
    assert ex.cash() == pytest.approx(100.3)
    assert db.get("realized_pnl") == pytest.approx(0.3)
    assert any("locked" in n for n in notes)


def test_mm_pulls_quotes_on_price_jump(env):
    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0)]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    assert ex.open_orders()
    pub.set_book("Y1", 0.59, 0.61)
    pub.set_book("N1", 0.39, 0.41)
    mm.step()
    assert ex.open_orders() == []


def test_risk_limits_and_drawdown(env):
    s, db, pub, ex, risk, _ = env
    assert risk.allow_buy(10, exposure=85, cash=100)[0] is False  # cap is 90
    assert risk.allow_buy(10, exposure=0, cash=12)[0] is False  # keeps $5 reserve
    assert risk.allow_buy(10, exposure=0, cash=100)[0] is True
    assert risk.check_drawdown(100.0) is None
    assert "daily loss" in risk.check_drawdown(84.0)
    risk.kill("test")
    assert risk.allow_buy(1, 0, 100)[0] is False


class FakeAnalyst:
    def __init__(self, fc):
        self.fc = fc

    def forecast(self, **_):
        return self.fc


def _forecaster(env, fc):
    s, db, pub, ex, risk, notes = env
    return Forecaster(s, db, ex, risk, FakeAnalyst(fc), rss=None, x=None, notify=notes.append)


def test_forecaster_buys_underpriced_side(env):
    s, db, pub, ex, risk, notes = env
    m = make_market(price=0.30)
    pub.set_book("Y1", 0.29, 0.31)
    pub.set_book("N1", 0.69, 0.71)
    f = _forecaster(env, Forecast(probability_yes=0.55, confidence="high", already_priced_in=False,
                                  reasoning="New official statement."))
    f.evaluate(m, ["headline"])
    pos = db.position("Y1")
    assert pos and pos.avg_price == 0.31 and pos.target_price == 0.55
    assert pos.cost <= s.fc_max_trade_usd + 1e-9
    # Price reaches our estimate -> take profit.
    pub.set_book("Y1", 0.55, 0.57)
    f.manage_positions()
    assert db.position("Y1") is None
    assert db.get("realized_pnl") > 0


def test_forecaster_skips_when_priced_in_or_small_edge(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.29, 0.31)
    pub.set_book("N1", 0.69, 0.71)
    f = _forecaster(env, Forecast(probability_yes=0.60, confidence="high", already_priced_in=True,
                                  reasoning="x"))
    f.evaluate(make_market(mid="a"), ["h"])
    f2 = _forecaster(env, Forecast(probability_yes=0.33, confidence="high", already_priced_in=False,
                                   reasoning="x"))
    f2.evaluate(make_market(mid="b"), ["h"])
    assert db.positions() == []
    assert [r["action"] for r in db.q("SELECT action FROM signals")] == ["skip", "skip"]


def test_dashboard_serves_state():
    app = build_app(lambda: {"ok": 1}, {"pause": lambda: "paused"})
    c = TestClient(app)
    assert c.get("/api/state").json() == {"ok": 1}
    assert c.post("/api/pause").json() == {"ok": True, "result": "paused"}
    assert "polybot" in c.get("/").text


def test_rss_parsing_skips_stale_items():
    import time as _t
    from email.utils import formatdate

    from polybot.news import RSSSource

    xml = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
    <item><title>Fed holds rates</title><link>https://x/1</link><pubDate>{formatdate(_t.time() - 600)}</pubDate></item>
    <item><title>Old story</title><link>https://x/2</link><pubDate>{formatdate(_t.time() - 86400)}</pubDate></item>
    </channel></rss>""".encode()

    class Resp:
        content = xml

    src = RSSSource(["https://feed"])
    src.http.get = lambda url: Resp()
    items = src.fetch()
    assert [i.title for i in items] == ["Fed holds rates"]
    assert items[0].source == "Wire"


def test_keyword_prefilter_matches_specific_market(env):
    from polybot.news import NewsItem

    f = _forecaster(env, None)
    f.kw = {"a": {"powell", "federal", "reserve", "rates", "december"},
            "b": {"bitcoin", "100k", "december"}}
    item = NewsItem("1", "Wire", "Powell signals Federal Reserve may cut rates", "", "", 0)
    assert f.prefilter(item) == ["a"]


def test_mm_skips_wide_books_and_respects_budget(env):
    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(cid="wide", question="Wide", yes="WY", no="WN"),
                   make_market(cid="a", question="A", yes="AY", no="AN"),
                   make_market(cid="b", question="B", yes="BY", no="BN"),
                   make_market(cid="c", question="C", yes="CY", no="CN")]
    pub.rewards = [make_reward(cid=c, max_spread=4.0, min_size=15, rate=r)
                   for c, r in (("wide", 99), ("a", 30), ("b", 20), ("c", 10))]
    pub.set_book("WY", 0.30, 0.70)  # 40c wide: not quotable
    for t in ("AY", "BY", "CY"):
        pub.set_book(t, 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.select_markets()
    # Cap is $90 by default -> room for all; with a $35 cap only two 15-share markets fit.
    assert "wide" not in mm.markets and len(mm.markets) == 3
    s.max_total_exposure_usd = 35
    mm2 = MarketMaker(s, db, ex, risk, notes.append)
    mm2.select_markets()
    assert set(mm2.markets) == {"a", "b"}


def test_dust_order_does_not_move_adjusted_mid(env):
    s, db, pub, ex, risk, notes = env
    # Real book 0.40/0.60 with 100 shares; someone posts 1-share orders at 0.55/0.57.
    pub.set_levels("Y1", bids=[(0.40, 100), (0.55, 1)], asks=[(0.60, 100), (0.57, 1)])
    b = ex.book("Y1")
    assert b.mid == pytest.approx(0.56)
    assert b.adjusted_mid(20) == pytest.approx(0.50)


def test_mm_drops_market_after_repeated_jumps(env):
    s, db, pub, ex, risk, notes = env
    s.mm_cooldown_seconds = 0
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0)]
    pub.set_book("N1", 0.49, 0.51)
    pub.set_book("Y1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    for bid in (0.55, 0.49):  # two jumps: cool down but keep the market
        pub.set_book("Y1", bid, bid + 0.02)
        mm.step()
        assert "c1" in mm.markets
    pub.set_book("Y1", 0.55, 0.57)  # third jump within the hour: drop it
    mm.step()
    assert "c1" not in mm.markets and mm.last_select == 0.0
    assert any("dropping" in r["msg"] for r in db.q("SELECT msg FROM events"))


def test_mm_cancels_orders_left_from_previous_run(env):
    s, db, pub, ex, risk, notes = env
    s.max_total_exposure_usd = 40
    pub.set_book("OLD", 0.49, 0.51)
    ex.place_limit(token_id="OLD", side="BUY", price=0.45, size=60, strategy="mm",
                   market="old", outcome="Yes", post_only=True)  # $27 tied up by a dead run
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0, min_size=20)]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    tokens = sorted(o.token_id for o in ex.open_orders())
    assert tokens == ["N1", "Y1"]


def test_equity_does_not_count_resting_orders(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.49, 0.51)
    ex.place_limit(token_id="Y1", side="BUY", price=0.45, size=20, strategy="mm",
                   market="c1", outcome="Yes", post_only=True)
    assert ex.equity() == pytest.approx(100.0)
    ex.cancel_all()
    assert ex.equity() == pytest.approx(100.0)


def test_reward_formula_matches_polymarket_shape():
    from polybot.rewards import order_score, q_min

    assert order_score(0.0, 100, 0.04) == pytest.approx(100)  # at the mid: full score
    assert order_score(0.02, 100, 0.04) == pytest.approx(25)  # halfway out: a quarter
    assert order_score(0.04, 100, 0.04) == 0  # at the band edge: nothing
    assert q_min(30, 30, 0.5) == 30  # two-sided keeps the min
    assert q_min(30, 0, 0.5) == pytest.approx(10)  # one-sided counts a third
    assert q_min(30, 0, 0.95) == 0  # one-sided earns nothing near the extremes


def test_paper_accrues_estimated_rewards_and_ranks_by_competition(env):
    import time as _t

    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(cid="crowded", question="Crowded", yes="CY", no="CN"),
                   make_market(cid="quiet", question="Quiet", yes="QY", no="QN")]
    pub.rewards = [make_reward(cid="crowded", max_spread=4.0, min_size=20, rate=100),
                   make_reward(cid="quiet", max_spread=4.0, min_size=20, rate=40)]
    # Same price, but the crowded market has 5000 shares sitting near the mid.
    pub.set_levels("CY", bids=[(0.49, 5000)], asks=[(0.51, 5000)])
    pub.set_levels("QY", bids=[(0.49, 50)], asks=[(0.51, 50)])
    for t in ("CN", "QN"):
        pub.set_book(t, 0.49, 0.51)
    s.mm_max_markets = 1
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.select_markets()
    assert list(mm.markets) == ["quiet"]  # smaller pool, but a far bigger share of it
    mm.step()
    m = mm.markets["quiet"]
    m.extra["last_accrual"] = _t.time() - 20
    mm.step()
    today, total = db.rewards("est")
    assert today > 0 and total == pytest.approx(today)


def test_mm_skips_event_driven_markets(env):
    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(cid="cpi", question="Will Core CPI YoY be 2.6% in October?", yes="AY", no="AN"),
                   make_market(cid="pce", question="Will Core PCE YoY be 3.0% in September?", yes="CY", no="CN"),
                   make_market(cid="ok", question="Will Lazio qualify for the Champions League?", yes="BY", no="BN")]
    pub.rewards = [make_reward(cid="cpi", rate=200), make_reward(cid="pce", rate=150),
                   make_reward(cid="ok", rate=50)]
    for t in ("AY", "AN", "BY", "BN", "CY", "CN"):
        pub.set_book(t, 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.select_markets()
    assert list(mm.markets) == ["ok"]


def test_paper_startup_drops_stale_orders_without_filling(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.49, 0.51)
    ex.place_limit(token_id="Y1", side="BUY", price=0.365, size=20, strategy="mm",
                   market="c1", outcome="Yes", post_only=True)
    pub.set_book("Y1", 0.25, 0.27)  # price fell while the laptop was off
    assert ex.startup_cleanup() == 1
    assert ex.sync_fills() == [] and ex.open_orders() == []
    assert db.positions() == [] and ex.cash() == pytest.approx(100.0)


def test_mm_renews_quotes_before_they_expire(env):
    import time as _t
    from dataclasses import replace

    s, db, pub, ex, risk, notes = env
    pub.markets = [make_market(price=0.5)]
    pub.rewards = [make_reward(max_spread=4.0)]
    pub.set_book("Y1", 0.49, 0.51)
    pub.set_book("N1", 0.49, 0.51)
    mm = MarketMaker(s, db, ex, risk, notes.append)
    mm.step()
    first = {o.order_id for o in ex.open_orders()}
    # Pretend the venue says these expire in 60s, as live quotes would.
    real_open = ex.open_orders
    ex.open_orders = lambda token_id=None: [replace(o, expires_at=_t.time() + 60) for o in real_open(token_id)]
    mm.step()
    ex.open_orders = real_open
    renewed = {o.order_id for o in ex.open_orders()}
    assert len(renewed) == 2 and not (first & renewed)


def test_locked_client_passes_calls_through():
    from polybot.exchange import _Locked

    class Thing:
        x = 5

        def add(self, a, b=1):
            return a + b

    t = _Locked(Thing())
    assert t.x == 5 and t.add(2, b=3) == 5


def test_take_fills_available_and_leaves_nothing_resting(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.40, 0.42, size=8)  # only 8 shares offered at 0.42
    oid = ex.take(token_id="Y1", side="BUY", size=20, limit_price=0.42, strategy="fc",
                  market="m1", outcome="Yes")
    assert oid and db.position("Y1").size == 8 and ex.open_orders() == []
    # Price limit not met: nothing happens, nothing rests.
    assert ex.take(token_id="Y1", side="BUY", size=5, limit_price=0.41, strategy="fc",
                   market="m1", outcome="Yes") is None
    assert ex.open_orders() == []
    # Selling more than we hold only sells what we have.
    ex.take(token_id="Y1", side="SELL", size=50, limit_price=0.40, strategy="fc", market="m1", outcome="Yes")
    assert db.position("Y1") is None and ex.open_orders() == []


def test_book_cache_snapshot_gives_best_first_book():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.44, 20), (0.46, 100), (0.45, 50)], asks=[(0.49, 10), (0.47, 30)],
                           tick=0.001, min_size=5))
    b = cache.get("Y1")
    assert b.bids == [(0.46, 100), (0.45, 50), (0.44, 20)] and b.asks == [(0.47, 30), (0.49, 10)]
    assert (b.best_bid, b.bid_depth, b.best_ask, b.ask_depth) == (0.46, 100, 0.47, 30)
    assert (b.tick, b.min_size) == (0.001, 5)
    assert cache.get("N1") is None  # never had a snapshot


def test_book_cache_price_changes_set_totals_and_remove_empty_levels():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100), (0.45, 50)], asks=[(0.47, 30)]))
    cache.apply(change_event(
        ("Y1", "BUY", 0.46, 80),   # new total at 0.46, not +80
        ("Y1", "BUY", 0.45, 0),    # nobody left at 0.45
        ("Y1", "BUY", 0.44, 10),   # new level
        ("Y1", "SELL", 0.48, 25),  # SELL side updates the asks
    ))
    b = cache.get("Y1")
    assert b.bids == [(0.46, 80), (0.44, 10)]
    assert b.asks == [(0.47, 30), (0.48, 25)]


def test_book_cache_routes_changes_by_token_and_skips_tokens_without_snapshot():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]))
    cache.apply(book_event("N1", bids=[(0.53, 30)], asks=[(0.54, 100)]))
    # One event carries both sides of the market (YES bid mirrors NO ask), plus a token we never saw.
    cache.apply(change_event(("Y1", "BUY", 0.46, 90), ("N1", "SELL", 0.54, 90), ("X9", "BUY", 0.10, 5)))
    assert cache.get("Y1").bids == [(0.46, 90)]
    assert cache.get("N1").asks == [(0.54, 90)]
    assert cache.get("X9") is None  # a book built from changes alone would be incomplete


def test_book_cache_snapshot_replaces_book_and_keeps_known_tick():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100), (0.45, 50)], asks=[(0.47, 30)], tick=0.001, min_size=5))
    # The 0.45 bid was cancelled while we missed its change; the next snapshot must drop it.
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)], tick=None, min_size=None))
    b = cache.get("Y1")
    assert b.bids == [(0.46, 100)]
    assert (b.tick, b.min_size) == (0.001, 5)  # snapshot left them out: keep what we knew
    cache.apply(tick_event("Y1", 0.01))
    assert cache.get("Y1").tick == 0.01


def test_book_cache_get_returns_a_copy():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]))
    b = cache.get("Y1")
    cache.apply(change_event(("Y1", "BUY", 0.46, 0)))
    assert b.bids == [(0.46, 100)]  # the market maker's Book doesn't change under it
    assert cache.get("Y1").bids == []


def test_book_cache_is_unusable_when_stream_is_silent_or_cleared():
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]))
    cache.last_message -= 120  # nothing heard from the stream for 2 minutes
    assert cache.get("Y1") is None
    cache.apply(change_event(("Y1", "BUY", 0.46, 90)))  # stream is back
    assert cache.get("Y1").bids == [(0.46, 90)]
    cache.clear()  # e.g. after a disconnect
    assert cache.get("Y1") is None


def test_books_prefer_the_stream_cache(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.40, 0.42)  # what the REST API would say
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]))
    ex.cache = cache
    assert ex.books(["Y1"])["Y1"].best_bid == 0.46
    assert ex.book("Y1").best_bid == 0.46


def test_books_fall_back_to_rest_for_tokens_the_cache_lacks(env):
    s, db, pub, ex, risk, notes = env
    pub.set_book("Y1", 0.40, 0.42)
    pub.set_book("N1", 0.57, 0.59)
    cache = BookCache()
    cache.apply(book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]))
    ex.cache = cache
    books = ex.books(["Y1", "N1"])
    assert books["Y1"].best_bid == 0.46  # from the stream
    assert books["N1"].best_bid == 0.57  # from REST
    cache.last_message -= 120  # stream went quiet: everything comes from REST again
    assert ex.books(["Y1"])["Y1"].best_bid == 0.40
    assert ex.book("Y1").best_bid == 0.40


def _stream_until(cache, tokens, client, done, timeout=2.0):
    """Run the stream until done() is true, then stop it the way shutdown does (cancel)."""
    async def go():
        task = asyncio.create_task(run_market_stream(cache, tokens, client, poll=0.01, retry=0.01))
        deadline = asyncio.get_running_loop().time() + timeout
        while not done():
            assert asyncio.get_running_loop().time() < deadline, "stream never got there"
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_stream_feeds_the_cache_and_closes_on_shutdown():
    cache = BookCache()
    handle = FakeHandle([book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)])])
    client = FakeStreamClient(handle)
    _stream_until(cache, lambda: {"Y1", "N1"}, client, lambda: cache.get("Y1") is not None)
    assert client.subscribed == [["N1", "Y1"]]
    assert cache.get("Y1").best_bid == 0.46
    assert handle.closed


def test_stream_waits_for_tokens_then_follows_market_changes():
    cache = BookCache()
    first = FakeHandle([])  # a quiet market: no events at all
    client = FakeStreamClient(first, FakeHandle([]))
    calls = 0

    def tokens():
        nonlocal calls
        calls += 1
        if calls <= 3:
            return set()  # the market maker hasn't picked its markets yet
        if not client.subscribed:
            return {"Y1"}
        return {"Y1", "N1"}  # then it switches markets while the first subscription is quiet

    _stream_until(cache, tokens, client, lambda: len(client.subscribed) == 2)
    assert client.subscribed == [["Y1"], ["N1", "Y1"]]  # never subscribed to an empty list
    assert first.closed


def test_stream_error_clears_cache_and_resubscribes():
    cache = BookCache()
    broken = FakeHandle([book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)]), ConnectionError("dropped")])
    client = FakeStreamClient(broken, FakeHandle([]))
    _stream_until(cache, lambda: {"Y1"}, client, lambda: len(client.subscribed) == 2)
    assert cache.get("Y1") is None  # books from before the gap can't be trusted
    assert broken.closed


def test_stream_end_clears_cache_and_resubscribes():
    cache = BookCache()
    ended = FakeHandle([book_event("Y1", bids=[(0.46, 100)], asks=[(0.47, 30)])], end=True)
    client = FakeStreamClient(ended, FakeHandle([]))
    _stream_until(cache, lambda: {"Y1"}, client, lambda: len(client.subscribed) == 2)
    assert cache.get("Y1") is None  # events may have been missed before it ended
    assert ended.closed
