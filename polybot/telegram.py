"""Minimal Telegram bot over the HTTP API: sends alerts and answers commands from your
chat only. Commands: /status /positions /pause /resume /kill /help"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Handler = Callable[[str], Awaitable[str]]


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.enabled = bool(token and chat_id)
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=40)
        self.handlers: dict[str, Handler] = {}

    async def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram disabled] %s", text)
            return
        try:
            await self.http.post(f"{self.base}/sendMessage",
                                 json={"chat_id": self.chat_id, "text": text[:4000]})
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %s", e)

    def on(self, command: str, handler: Handler) -> None:
        self.handlers[command] = handler

    async def poll_forever(self) -> None:
        if not self.enabled:
            return
        offset = 0
        while True:
            try:
                r = await self.http.get(f"{self.base}/getUpdates",
                                        params={"offset": offset, "timeout": 30})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    # Only obey the configured chat; anyone else who finds the bot is ignored.
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    parts = (msg.get("text") or "").strip().split()
                    if not parts:
                        continue
                    cmd = parts[0].split("@")[0].lstrip("/").lower()
                    handler = self.handlers.get(cmd) or self.handlers.get("help")
                    if handler:
                        await self.send(await handler(" ".join(parts[1:])))
            except Exception as e:  # network blips must never kill the bot
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(5)
