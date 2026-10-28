"""SQLite store: fills, positions, orders, seen news, Claude spend, daily equity, flags."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, token_id TEXT, market TEXT,
    side TEXT, price REAL, size REAL, order_id TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY, market TEXT, outcome TEXT, strategy TEXT,
    size REAL, avg_price REAL, target_price REAL, opened_ts REAL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY, ts REAL, strategy TEXT, token_id TEXT, market TEXT,
    side TEXT, price REAL, size REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS seen_news (id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, ts REAL, source TEXT, title TEXT, summary TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, ts REAL, market TEXT, question TEXT, prob REAL, price REAL,
    edge REAL, confidence TEXT, reasoning TEXT, action TEXT
);
CREATE TABLE IF NOT EXISTS claude_spend (day TEXT PRIMARY KEY, usd REAL);
CREATE TABLE IF NOT EXISTS equity (day TEXT PRIMARY KEY, start_equity REAL, last_equity REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS rewards (day TEXT, kind TEXT, usd REAL, PRIMARY KEY (day, kind));
"""


@dataclass
class Position:
    token_id: str
    market: str
    outcome: str
    strategy: str
    size: float
    avg_price: float
    target_price: float | None
    opened_ts: float

    @property
    def cost(self) -> float:
        return self.size * self.avg_price


def utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, args)

    # --- key/value flags ---------------------------------------------------------
    def get(self, k: str, default=None):
        rows = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, k: str, v) -> None:
        self.x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # --- events log ------------------------------------------------------------------
    def event(self, level: str, msg: str) -> None:
        self.x("INSERT INTO events (ts, level, msg) VALUES (?, ?, ?)", (time.time(), level, msg))

    # --- positions -------------------------------------------------------------------
    def positions(self, strategy: str | None = None) -> list[Position]:
        sql = "SELECT * FROM positions WHERE size > 1e-9"
        args: tuple = ()
        if strategy:
            sql += " AND strategy=?"
            args = (strategy,)
        return [Position(**dict(r)) for r in self.q(sql, args)]

    def position(self, token_id: str) -> Position | None:
        rows = self.q("SELECT * FROM positions WHERE token_id=?", (token_id,))
        return Position(**dict(rows[0])) if rows and rows[0]["size"] > 1e-9 else None

    def record_fill(
        self,
        *,
        strategy: str,
        token_id: str,
        market: str,
        outcome: str,
        side: str,
        price: float,
        size: float,
        order_id: str = "",
        target_price: float | None = None,
    ) -> float:
        """Store a fill and update the position. Returns realized PnL of this fill."""
        self.x(
            "INSERT INTO fills (ts, strategy, token_id, market, side, price, size, order_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), strategy, token_id, market, side, price, size, order_id),
        )
        pos = self.position(token_id)
        realized = 0.0
        if side == "BUY":
            if pos:
                new_size = pos.size + size
                avg = (pos.cost + price * size) / new_size
                self.x(
                    "UPDATE positions SET size=?, avg_price=?, target_price=COALESCE(?, target_price)"
                    " WHERE token_id=?",
                    (new_size, avg, target_price, token_id),
                )
            else:
                self.x(
                    "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (token_id, market, outcome, strategy, size, price, target_price, time.time()),
                )
        else:
            if not pos:
                return 0.0
            sold = min(size, pos.size)
            realized = (price - pos.avg_price) * sold
            self.x("UPDATE positions SET size=? WHERE token_id=?", (pos.size - sold, token_id))
        if realized:
            self.set("realized_pnl", self.get("realized_pnl", 0.0) + realized)
        return realized

    # --- Claude spend ----------------------------------------------------------------
    def add_claude_spend(self, usd: float) -> None:
        day = utc_day()
        self.x(
            "INSERT INTO claude_spend (day, usd) VALUES (?, ?)"
            " ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd",
            (day, usd),
        )

    def claude_spend_today(self) -> float:
        rows = self.q("SELECT usd FROM claude_spend WHERE day=?", (utc_day(),))
        return rows[0]["usd"] if rows else 0.0

    # --- news ------------------------------------------------------------------
    def mark_seen(self, item_id: str) -> bool:
        """Returns True if the item is new."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen_news (id, ts) VALUES (?, ?)", (item_id, time.time())
            )
            return cur.rowcount == 1

    # --- equity tracking ------------------------------------------------------------
    def update_equity(self, equity: float) -> float:
        """Record equity; returns today's starting equity."""
        day = utc_day()
        self.x(
            "INSERT INTO equity (day, start_equity, last_equity) VALUES (?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET last_equity = excluded.last_equity",
            (day, equity, equity),
        )
        return self.q("SELECT start_equity FROM equity WHERE day=?", (day,))[0]["start_equity"]

    # --- liquidity rewards ("est" = our formula estimate, "real" = paid by Polymarket) ----
    def add_reward(self, kind: str, usd: float, day: str | None = None) -> None:
        self.x(
            "INSERT INTO rewards (day, kind, usd) VALUES (?, ?, ?)"
            " ON CONFLICT(day, kind) DO UPDATE SET usd = usd + excluded.usd",
            (day or utc_day(), kind, usd),
        )

    def set_reward(self, kind: str, usd: float, day: str) -> None:
        self.x("INSERT OR REPLACE INTO rewards (day, kind, usd) VALUES (?, ?, ?)", (day, kind, usd))

    def rewards(self, kind: str) -> tuple[float, float]:
        """(today, all time)"""
        today = self.q("SELECT usd FROM rewards WHERE day=? AND kind=?", (utc_day(), kind))
        total = self.q("SELECT COALESCE(SUM(usd), 0) AS t FROM rewards WHERE kind=?", (kind,))
        return (today[0]["usd"] if today else 0.0), total[0]["t"]
