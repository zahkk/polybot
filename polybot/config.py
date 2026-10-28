"""All settings come from environment variables (or a .env file next to the code).

Every money value is in USD. Defaults are sized for a ~$100-150 account.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mode -----------------------------------------------------------------
    # "paper" = real market data, simulated orders. "live" = real money.
    mode: str = Field("paper", pattern="^(paper|live)$")
    paper_starting_cash: float = 100.0

    # --- Polymarket ---------------------------------------------------------------
    poly_private_key: str = ""
    # Your Polymarket wallet (the address shown on your Polymarket profile / deposit
    # page). Leave empty to let the SDK use the signer's default deposit wallet.
    poly_wallet_address: str = ""
    # Builder API key for gasless actions (trading approvals, merging YES+NO pairs).
    # `python -m polybot.check --approve` creates one and saves it here automatically.
    poly_builder_key: str = ""
    poly_builder_secret: str = ""
    poly_builder_passphrase: str = ""

    # --- Claude -----------------------------------------------------------------
    anthropic_api_key: str = ""
    # Cheap model that filters headlines -> possibly relevant markets.
    screen_model: str = "claude-haiku-5-5"
    # Stronger model that estimates a probability before any trade.
    analyst_model: str = "claude-opus-5-5"
    analyst_effort: str = "medium"
    # Hard cap on Claude spend per UTC day. The forecaster stops calling Claude once hit.
    claude_daily_budget_usd: float = 0.50

    # --- Telegram ---------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- X / Twitter (optional, costs money per post read) ----------------------
    x_bearer_token: str = ""
    x_accounts: str = ""  # comma-separated usernames, e.g. "Polymarket,Kalshi,DeItaone"
    x_poll_seconds: int = 300

    # --- Risk -------------------------------------------------------------------
    max_total_exposure_usd: float = 90.0  # cost basis of all positions + resting buy orders
    daily_loss_limit_usd: float = 15.0  # equity drop from start of UTC day -> kill switch
    min_cash_reserve_usd: float = 5.0

    # --- Market making ----------------------------------------------------------
    mm_enabled: bool = True
    mm_max_markets: int = 3
    mm_max_per_market_usd: float = 25.0
    mm_refresh_seconds: int = 20
    # Live quotes expire after this long (Polymarket minimum is 3 minutes) and are renewed
    # 2 minutes before. If the bot dies, its orders disappear within this time.
    order_ttl_seconds: int = 600
    # On shutdown, sell any shares the bot holds so nothing sits unwatched while the laptop is off.
    close_positions_on_shutdown: bool = True
    # Stop by itself after this many minutes (0 = run until stopped). run.bat asks for it.
    run_for_minutes: float = 0
    # Short-dated markets are mostly scheduled data releases or events that jump on resolution.
    mm_min_days_to_end: float = 21.0
    mm_price_band: tuple[float, float] = (0.15, 0.85)  # only quote markets with mid in this band
    # Fraction of the reward band to sit inside. 0.5 = halfway between mid and band edge.
    mm_spread_fraction: float = 0.5
    # If the midpoint jumps this much between refreshes, pull quotes and cool down.
    mm_jump_threshold: float = 0.03
    mm_cooldown_seconds: int = 600
    # Exit an unhedged leg if price moves this far against our average cost.
    mm_stop_loss: float = 0.08
    # Skip markets driven by scheduled events or insiders: informed traders pick off quotes.
    mm_avoid_keywords: str = (
        "cpi,pce,yoy,pmi,inflation,payrolls,nonfarm,jobs report,unemployment rate,fomc,federal reserve,fed rate,interest rate,"
        "gdp,earnings,posts,tweets,outage,downtime,token,fdv,airdrop,launch,listing,price of,above $,below $"
    )

    # --- News forecasting ---------------------------------------------------------
    fc_enabled: bool = True
    fc_poll_seconds: int = 120
    fc_universe_size: int = 400
    fc_min_volume_usd: float = 5_000
    fc_min_edge: float = 0.08  # model prob minus price needed to trade
    fc_max_trade_usd: float = 5.0
    fc_kelly_fraction: float = 0.15
    fc_max_open_positions: int = 6
    fc_min_confidence: str = "medium"

    # --- Ops ------------------------------------------------------------------
    db_path: str = "polybot.db"
    dashboard_host: str = "127.0.0.1"  # keep on localhost; reach it with an SSH tunnel
    dashboard_port: int = 8080
    feeds_file: str = "feeds.txt"
    log_level: str = "INFO"
    summary_hour_utc: int = 6  # daily Telegram summary time, in UTC

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def mm_avoid_list(self) -> list[str]:
        return [k.strip().lower() for k in self.mm_avoid_keywords.split(",") if k.strip()]

    @property
    def x_account_list(self) -> list[str]:
        return [a.strip().lstrip("@") for a in self.x_accounts.split(",") if a.strip()]

    @property
    def builder_api_key(self):
        if not (self.poly_builder_key and self.poly_builder_secret and self.poly_builder_passphrase):
            return None
        from polymarket import BuilderApiKey

        return BuilderApiKey(key=self.poly_builder_key, secret=self.poly_builder_secret,
                             passphrase=self.poly_builder_passphrase)
