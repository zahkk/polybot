"""News sources. RSS is free; X (Twitter) is optional and billed per post read, so it
only fetches posts newer than the last one it saw."""

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import httpx

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://rss.politico.com/politics-news.xml",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.espn.com/espn/rss/news",
]


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    summary: str
    url: str
    ts: float


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def load_feeds(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return DEFAULT_FEEDS
    lines = [ln.strip() for ln in p.read_text().splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


class RSSSource:
    def __init__(self, feeds: list[str]):
        self.feeds = feeds
        self.http = httpx.Client(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 polybot"})

    def fetch(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for url in self.feeds:
            try:
                parsed = feedparser.parse(self.http.get(url).content)
            except Exception as e:
                log.debug("feed %s failed: %s", url, e)
                continue
            source = parsed.feed.get("title", url)[:40]
            for e in parsed.entries[:30]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                ts = time.mktime(e.published_parsed) if e.get("published_parsed") else time.time()
                if time.time() - ts > 6 * 3600:  # stale; markets have already priced it
                    continue
                summary = (e.get("summary") or "")[:500]
                items.append(NewsItem(_id(url, e.get("link", title)), source, title, summary,
                                      e.get("link", ""), ts))
        return items


class XSource:
    """Reads recent posts from a list of accounts using X API v2 with a bearer token."""

    API = "https://api.x.com/2"

    def __init__(self, bearer: str, usernames: list[str], db):
        self.http = httpx.Client(timeout=15, headers={"Authorization": f"Bearer {bearer}"})
        self.usernames = usernames
        self.db = db
        self.last_poll = 0.0

    def _user_ids(self) -> dict[str, str]:
        cached = self.db.get("x_user_ids", {}) or {}
        missing = [u for u in self.usernames if u not in cached]
        if missing:
            r = self.http.get(f"{self.API}/users/by", params={"usernames": ",".join(missing)})
            for u in r.json().get("data", []):
                cached[u["username"]] = u["id"]
            self.db.set("x_user_ids", cached)
        return {u: cached[u] for u in self.usernames if u in cached}

    def fetch(self, poll_seconds: int) -> list[NewsItem]:
        if time.time() - self.last_poll < poll_seconds:
            return []
        self.last_poll = time.time()
        items: list[NewsItem] = []
        since = self.db.get("x_since_ids", {}) or {}
        try:
            ids = self._user_ids()
        except Exception as e:
            log.warning("x: user lookup failed: %s", e)
            return []
        for username, uid in ids.items():
            params = {"max_results": 10, "exclude": "retweets,replies",
                      "tweet.fields": "created_at"}
            if since.get(uid):
                params["since_id"] = since[uid]
            try:
                r = self.http.get(f"{self.API}/users/{uid}/tweets", params=params)
                r.raise_for_status()
            except Exception as e:
                log.warning("x: @%s fetch failed: %s", username, e)
                continue
            body = r.json()
            for t in body.get("data", []):
                items.append(NewsItem(_id("x", t["id"]), f"@{username}", t["text"][:280], "",
                                      f"https://x.com/{username}/status/{t['id']}", time.time()))
            newest = body.get("meta", {}).get("newest_id")
            if newest:
                since[uid] = newest
        self.db.set("x_since_ids", since)
        return items
