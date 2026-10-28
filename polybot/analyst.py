"""Claude calls. A cheap model screens headlines against markets; a stronger model
estimates a probability only for the few pairs that pass. Spend is metered per call
and capped per day."""

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

# USD per million tokens (input, output).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
}
# Server-side refusal fallback is available on these models, not on Haiku.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"}


class ScreenMatch(BaseModel):
    headline_id: str
    market_ids: list[str] = Field(description="Markets whose outcome this headline could materially change")


class ScreenResult(BaseModel):
    matches: list[ScreenMatch]


class Forecast(BaseModel):
    probability_yes: float = Field(description="Calibrated probability the market resolves YES, 0 to 1")
    confidence: Literal["low", "medium", "high"]
    already_priced_in: bool = Field(description="True if the market price likely already reflects this news")
    reasoning: str = Field(description="Two to four sentences")


SCREEN_SYSTEM = """You match breaking news to prediction markets.
For each headline, list only the markets whose resolution the headline could directly and
materially change (it is about the same people, event, deadline, or number). Topical
similarity is not enough. Most headlines match nothing; return an empty list for those."""

ANALYST_SYSTEM = """You are a careful, calibrated forecaster pricing a Polymarket market.
Read the resolution rules closely: markets resolve on their exact wording, not on the spirit.
The current market price is a strong prior made by people with money at stake; move away
from it only when the news gives a concrete reason the crowd may not have absorbed yet.
If the news is old, vague, or already reflected in the price, say so and stay near the
market price. Never invent facts that are not in the provided material."""


class Analyst:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, max_retries=3)

    def budget_left(self) -> float:
        return self.s.claude_daily_budget_usd - self.db.claude_spend_today()

    def _meter(self, resp) -> None:
        inp, out = PRICES.get(resp.model, PRICES["claude-opus-5-5"])
        u = resp.usage
        tokens_in = (u.input_tokens or 0) + (u.cache_creation_input_tokens or 0) * 1.25 \
            + (u.cache_read_input_tokens or 0) * 0.1
        cost = tokens_in * inp / 1e6 + (u.output_tokens or 0) * out / 1e6
        self.db.add_claude_spend(cost)
        log.debug("claude %s cost $%.4f", resp.model, cost)

    def _parse(self, *, model: str, system: str, prompt: str, schema, effort: str, max_tokens: int):
        kwargs = {}
        if model in FALLBACK_MODELS:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        resp = self.client.beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort},
            output_format=schema,
            **kwargs,
        )
        self._meter(resp)
        if resp.stop_reason == "refusal":
            log.warning("claude refused (%s)", getattr(resp.stop_details, "category", None))
            return None
        if resp.stop_reason == "max_tokens":
            log.warning("claude hit max_tokens")
            return None
        return resp.parsed_output

    def screen(self, headlines: list[dict], markets: list[dict]) -> list[ScreenMatch]:
        """headlines: [{id, text}], markets: [{id, question}]"""
        if not headlines or not markets or self.budget_left() <= 0:
            return []
        prompt = "HEADLINES:\n" + "\n".join(f"[{h['id']}] {h['text']}" for h in headlines)
        prompt += "\n\nMARKETS:\n" + "\n".join(f"[{m['id']}] {m['question']}" for m in markets)
        try:
            result = self._parse(model=self.s.screen_model, system=SCREEN_SYSTEM, prompt=prompt,
                                 schema=ScreenResult, effort="low", max_tokens=4000)
        except anthropic.APIError as e:
            log.warning("screen failed: %s", e)
            return []
        return result.matches if result else []

    def forecast(self, *, question: str, rules: str, end_date: str, yes_bid: float | None,
                 yes_ask: float | None, headlines: list[str]) -> Forecast | None:
        if self.budget_left() <= 0:
            return None
        prompt = (
            f"MARKET: {question}\n"
            f"CLOSES: {end_date}\n"
            f"RESOLUTION RULES:\n{rules[:4000]}\n\n"
            f"CURRENT PRICE OF YES: bid {yes_bid}, ask {yes_ask}\n\n"
            "RECENT NEWS (newest first):\n" + "\n".join(f"- {h}" for h in headlines[:12]) +
            "\n\nEstimate the probability this market resolves YES."
        )
        try:
            return self._parse(model=self.s.analyst_model, system=ANALYST_SYSTEM, prompt=prompt,
                               schema=Forecast, effort=self.s.analyst_effort, max_tokens=16000)
        except anthropic.APIError as e:
            log.warning("forecast failed: %s", e)
            return None
