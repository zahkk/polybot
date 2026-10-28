# polybot: context for Claude

Polymarket liquidity-rewards market-making bot. Read this before changing anything.
Personal/owner context lives in `CLAUDE.local.md` (gitignored, never committed). Read it if present.

## Ground rules
- Never suggest routing around a geoblock. The bot refuses to trade live where Polymarket blocks it.
- **Never print, log, or echo values from `.env`** (Telegram token, keys). Private keys are only ever
  typed into `.env` by the person running the bot.
- Running costs matter for small accounts: prefer free/local options until a strategy is proven.

## Architecture
- `polybot/exchange.py`: `PaperExchange` / `LiveExchange` share one interface, built on Polymarket's
  official unified SDK `polymarket-client` (the old `py-clob-client` packages are dead since CLOB V2,
  April 2026). Books use an **adjusted midpoint** that ignores orders below the reward min size.
- `polybot/market_maker.py`: picks reward markets (top 80 payers -> tight two-sided book inside the
  reward band -> skip event-driven keywords and markets ending within 21 days -> rank by estimated
  reward per dollar -> as many as the exposure cap funds). Quotes BUY YES and BUY NO; merges completed
  pairs; stop-loss on one-sided inventory; pulls quotes on 3c jumps; drops a market after 3 jumps/hour.
- `polybot/rewards.py`: Polymarket's reward scoring formula; paper mode accrues estimated rewards,
  live mode also records what Polymarket paid.
- `polybot/forecaster.py` + `analyst.py`: RSS/X headlines -> Haiku screen -> Opus probability ->
  buy if edge >= 8c. Off unless an Anthropic key is set.
- `polybot/risk.py`: exposure cap, cash reserve, daily-loss kill switch. `main.py` wires loops,
  Telegram, dashboard (`dashboard.py`), daily summary (`SUMMARY_HOUR_UTC`).
- `polybot/check.py`: pre-flight (location, wallet, balance, allowances); `--approve` creates a Builder
  API key (saved to `.env`) and sets up gasless trading approvals.
- `polybot.db` (SQLite) is paper/live state; deleting it resets paper mode. Don't confuse it with
  the code file `polybot/db.py`.

## Lessons from real runs
- Thin reward markets have jumpy raw midpoints (dust orders) -> adjusted midpoint.
- Orders from a previous process blocked the exposure cap -> selection cancels any MM order outside
  the chosen markets; `startup_cleanup()` handles leftovers (live records real offline fills first).
- Equity once double-counted cash in resting orders. Resting orders never reduce cash.
- Live MM quotes carry a 10-minute expiry (`ORDER_TTL_SECONDS`), renewed 2 minutes early, so a
  sleeping/crashed machine leaves nothing on the book. One-off trades use fill-and-kill (`take()`).
- Sessions: `run.bat` asks for minutes (Enter = until /stop); `/stopin N` changes it from Telegram.
  Shutdown (`/stop`, dashboard "Stop & close", Ctrl+C, or the timer) cancels orders and sells
  bot-held shares. Live `cancel_all` only touches the bot's own orders (manual orders are left alone).
- WinError 10035 came from threads sharing one HTTP/2 connection; SDK clients are wrapped in `_Locked`.
- `setup_trading_approvals` checks the chain directly; `get_trading_approvals_state` reads an indexer
  that lags. The trading server's balance-allowance view is what decides whether orders are accepted.

## Working on it
- Run `python -m pytest -q` after every change (tests use an in-memory fake Polymarket in `tests/fakes.py`).
- Windows users update with `update.bat` (git pull) and start with `start.bat` / `run.bat`.
- Push to `main` only after tests pass.
