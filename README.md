# polybot

A Polymarket bot that runs two strategies around the clock on a small VPS:

1. **Rewards market maker** places small buy orders on both YES and NO near the midpoint
   of markets that pay Polymarket liquidity rewards. If both sides fill, it merges the
   pair back into cash at a locked profit. If one side fills, it offers that side back
   above cost, and it cuts the position if the price moves too far against it.
2. **News forecaster** reads RSS feeds (and optionally X accounts). Claude Haiku matches
   new headlines to markets. Claude Opus then estimates a probability, and the bot buys the
   underpriced side only if its edge is at least 8¢. It sells once the price reaches
   Opus's estimate.

Paper mode is the default: it uses real market data and simulated orders.

Paper mode estimates the liquidity rewards your quotes would earn, using Polymarket's published
scoring formula; live mode also shows what Polymarket actually paid. Judge the bot on
**Net (realized + rewards)**, not on fills alone.

> **Realistic expectations:** this is a careful, small-stakes bot, not a money printer.
> Most retail bots lose money. Run paper mode for 1–2 weeks first, then go live with
> money you can afford to lose. Paper fills are optimistic: they ignore your place in
> the order queue, and paper mode doesn't estimate liquidity rewards.

## What you need

| Thing | Where | Cost |
|---|---|---|
| VPS (Ubuntu 24.04, 2 GB RAM) | Hetzner (Helsinki) or DigitalOcean. **Not** US/UK/France/Germany, which Polymarket blocks | ~$5/mo |
| Anthropic API key | console.anthropic.com → API keys, add $5–10 credit | capped by `CLAUDE_DAILY_BUDGET_USD` (default $0.50/day) |
| Telegram bot | Message **@BotFather** → `/newbot` → copy the token. Message **@userinfobot** to get your chat ID. Then send your new bot any message so it can reply | free |
| Polymarket account + funds | Your existing account. Export its private key: Polymarket → Profile → Settings → Export Private Key | your capital |
| X API (optional) | developer.x.com, pay-per-use credits | ~$0.005 per post read |

## Run on Windows

Double-click `start.bat`. The first run installs everything and opens `.env` for you to fill in;
after that it starts the bot and opens the dashboard. Close the window to stop it.

## Install on the VPS

From your computer, copy the folder to the server, then log in:

```bash
scp -r polybot root@YOUR_VPS_IP:/root/
ssh root@YOUR_VPS_IP
bash /root/polybot/deploy/setup.sh
nano /home/polybot/polybot/.env      # fill in keys (see .env.example)
systemctl start polybot
journalctl -u polybot -f             # live logs; Ctrl+C to stop watching
```

The bot restarts automatically if it crashes or the server reboots.

## Going live

1. Run in paper mode for 1–2 weeks. Check the daily Telegram summary and the dashboard.
2. Deposit funds to Polymarket. Put `POLY_PRIVATE_KEY` and `POLY_WALLET_ADDRESS` in `.env`.
3. Run the pre-flight check (it places no orders):
   ```bash
   cd /home/polybot/polybot && sudo -u polybot .venv/bin/python -m polybot.check
   ```
   If it reports missing approvals, run it again with `--approve`.
4. Set `MODE=live` in `.env` and run `systemctl restart polybot`.

## Controls

**Telegram:** `/status` `/positions` `/pause` `/resume` `/kill` (kill cancels every order and stops trading)

**Dashboard:** it only listens on the server itself. View it from your laptop with:
```bash
ssh -L 8080:localhost:8080 root@YOUR_VPS_IP
```
and open http://localhost:8080. It shows equity, P&L, positions, orders, Claude's
reasoning for each signal, fills, and has pause/resume/kill buttons.

**Automatic safety:**
- Total exposure cap (`MAX_TOTAL_EXPOSURE_USD`)
- $5 cash reserve
- $5 max per forecaster trade
- Kill switch if equity drops $15 in a UTC day
- Market-making quotes are pulled when a price jumps 3¢ (usually news)
- Live mode refuses to start if Polymarket geoblocks the server

## Tuning (in `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MM_MAX_MARKETS` | 3 | markets quoted at once |
| `MM_MAX_PER_MARKET_USD` | 25 | skips reward markets whose minimum order size costs more |
| `FC_MIN_EDGE` | 0.08 | Opus probability minus price needed to buy |
| `FC_MAX_TRADE_USD` | 5 | max spend per forecaster trade |
| `CLAUDE_DAILY_BUDGET_USD` | 0.50 | forecaster stops calling Claude after this |
| `X_ACCOUNTS` | | comma-separated usernames; only used if `X_BEARER_TOKEN` is set |

Add or remove news sources in `feeds.txt`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Code map: `polybot/market_maker.py`, `forecaster.py` (strategies) · `exchange.py`
(paper/live orders via Polymarket's official `polymarket-client` SDK) · `risk.py` ·
`analyst.py` (Claude) · `news.py` · `telegram.py` · `dashboard.py` · `main.py`.
