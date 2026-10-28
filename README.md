# polybot

A Polymarket bot that earns **liquidity rewards** by market making, with an optional AI news trader.

- **Rewards market maker (main strategy).** Polymarket pays daily rewards to resting orders near the
  midpoint of selected markets. The bot picks the best-paying markets it can afford, skips risky ones,
  and places small BUY orders on both YES and NO. If both fill, the pair is merged back into cash at a
  locked profit. If only one fills, it offers that side back above cost and cuts it if the price moves
  too far against it.
- **News forecaster (optional, off without an Anthropic key).** Reads RSS/X headlines; Claude Haiku
  matches them to markets, Claude Opus estimates a probability, and the bot buys only with an edge of
  at least 8¢.

**Paper mode is the default**: real market data, simulated orders, and estimated rewards using
Polymarket's published scoring formula. Judge it on **Net (realized + rewards)** on the dashboard.

> **Realistic expectations:** this is a careful, small-stakes bot, not a money printer. A good result is
> a few percent a month, and market making can lose money when prices move against you. Paper fills are
> optimistic (they ignore queue position) and reward estimates are approximate. Go live only with money
> you can afford to lose.

## Quick start (Windows)

1. Install **Python 3.12+** from python.org (tick "Add python.exe to PATH") and **Git**
   (`winget install --id Git.Git -e`).
2. Get the code: `git clone https://github.com/zahkk/polybot.git` and open the `polybot` folder.
3. Double-click **`start.bat`**. The first run installs everything and opens `.env`: fill in at least
   the Telegram settings (below), save, and run `start.bat` again.
4. The dashboard opens at http://localhost:8080 and Telegram says "polybot started in PAPER mode".

**Running it while you're away:** double-click **`run.bat`** and type how many minutes to run, or press
Enter to run until you stop it. When it stops it cancels its orders and sells anything it holds.

**Stopping it properly:** `/stop` in Telegram, the **Stop & close** button on the dashboard, or Ctrl+C.
Don't just close the window: that skips selling (orders still expire on their own within 10 minutes).

**Updating:** stop the bot, double-click **`update.bat`**, start again.

Keep the computer awake while it runs (Windows sleep: Never, lid open).

## What you need

| Thing | Where | Cost |
|---|---|---|
| Telegram bot | Message **@BotFather** → `/newbot` → copy the token. Message **@userinfobot** for your chat ID. Send your new bot any message so it can reply | free |
| Polymarket account + funds (live mode only) | Polymarket → Profile → Settings → **Export Private Key** | your capital |
| Anthropic API key (optional, news forecaster) | console.anthropic.com → API keys | capped by `CLAUDE_DAILY_BUDGET_USD` (default $0.50/day) |
| VPS (optional, to run 24/7) | Any Ubuntu 24.04 server with 2 GB RAM, in a country where Polymarket is allowed | ~$5–6/mo |

Polymarket blocks some countries (including the US and UK). The bot checks this on startup and
refuses to trade live from a blocked location. Use it only where you're allowed to.

## Going live

1. Run paper mode for at least a few sessions and check **Net (realized + rewards)**.
2. Put `POLY_PRIVATE_KEY` and `POLY_WALLET_ADDRESS` in `.env` (never share these or paste them anywhere).
3. Pre-flight check (places no orders):
   ```
   .venv\Scripts\python -m polybot.check
   ```
   If the **"Exchange allowances (trading server)"** line isn't ✓, run it again with `--approve`.
   That creates a Builder API key (saved to `.env`) and sets up the one-time trading approvals.
4. Delete `polybot.db` (clears paper stats), set `MODE=live` in `.env`, and start the bot.
5. Watch the first session: the bot's orders should appear under **Open orders** on polymarket.com.

While the bot is live, don't place manual trades on the same account: spending cash looks like a loss
to the bot and can trip the daily loss limit. (The bot never touches your manual orders or positions.)

## Controls

**Telegram:** `/status` `/positions` `/pause` `/resume` `/kill` `/stop` `/stopin <minutes>`
- `/kill` cancels every order and stops trading (bot keeps running); `/resume` restarts trading.
- `/stop` shuts the bot down cleanly; `/stopin 30` stops it in 30 minutes, `/stopin 0` cancels the timer.

**Dashboard** (http://localhost:8080): equity, P&L, estimated and paid rewards, positions, orders,
fills, events, and Pause / Resume / Kill / Stop & close buttons.

**Automatic safety:**
- Exposure cap (`MAX_TOTAL_EXPOSURE_USD`) and cash reserve (`MIN_CASH_RESERVE_USD`)
- Daily loss kill switch (`DAILY_LOSS_LIMIT_USD`)
- Skips markets ending within 21 days and event-driven markets (data releases, outages, token launches…)
- Pulls quotes when a price jumps 3¢; drops a market that jumps 3 times in an hour
- Live quotes expire after 10 minutes and are renewed, so a crashed or sleeping computer leaves
  nothing on the book; leftovers are cleaned up on startup
- One-off trades are fill-and-kill: nothing unintended is left resting
- Refuses to trade live from a geoblocked location

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MODE` | paper | `paper` or `live` |
| `MAX_TOTAL_EXPOSURE_USD` | 90 | max money in positions + resting buy orders |
| `DAILY_LOSS_LIMIT_USD` | 15 | equity drop in a UTC day that trips the kill switch |
| `MM_MAX_MARKETS` | 3 | markets quoted at once (also limited by the exposure cap) |
| `MM_MAX_PER_MARKET_USD` | 25 | skips reward markets whose minimum order size costs more |
| `MM_SPREAD_FRACTION` | 0.5 | how far inside the reward band to quote (lower = closer to mid = more rewards, more fills) |
| `MM_MIN_DAYS_TO_END` | 21 | skip markets resolving sooner than this |
| `CLOSE_POSITIONS_ON_SHUTDOWN` | true | sell bot-held shares when stopping |
| `RUN_FOR_MINUTES` | 0 | stop by itself after this long (0 = until stopped); `run.bat` asks for it |
| `FC_MIN_EDGE` / `FC_MAX_TRADE_USD` | 0.08 / 5 | news forecaster edge and trade size |
| `CLAUDE_DAILY_BUDGET_USD` | 0.50 | forecaster stops calling Claude after this |
| `SUMMARY_HOUR_UTC` | 6 | time of the daily Telegram summary |

Sizes should match your balance: roughly exposure cap ≈ 85% of balance, daily loss limit ≈ 15%.
News sources live in `feeds.txt`.

## Running 24/7 on a Linux VPS (optional)

```
scp -r polybot root@YOUR_VPS_IP:/root/
ssh root@YOUR_VPS_IP
bash /root/polybot/deploy/setup.sh
nano /home/polybot/polybot/.env
systemctl start polybot
journalctl -u polybot -f
```
View the dashboard with `ssh -L 8080:localhost:8080 root@YOUR_VPS_IP` and open http://localhost:8080.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```
Code map: `market_maker.py` + `rewards.py` (main strategy) · `forecaster.py` + `analyst.py` (news) ·
`exchange.py` (paper/live orders via Polymarket's official `polymarket-client` SDK) · `risk.py` ·
`check.py` (pre-flight) · `telegram.py` · `dashboard.py` · `main.py`. See `CLAUDE.md` for design notes.
