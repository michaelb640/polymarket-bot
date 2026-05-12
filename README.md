# Polymarket BTC Intraday Bot

An intraday trading bot for Polymarket's Bitcoin daily price markets. It uses a fair-value model based on BTC's current price, the market's strike price, and realized volatility to identify mispriced YES/NO tokens.

---

## Prerequisites

- Python 3.11+
- A Polygon wallet funded with USDC (for collateral) and MATIC (for gas)
- Polymarket API credentials

---

## 1. Install Dependencies

```bash
cd polymarket-btc-bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Get Polymarket API Credentials

1. Go to [polymarket.com](https://polymarket.com) and connect your wallet.
2. Navigate to **Profile → API Keys** and create a new key.
3. Save the **API Key**, **API Secret**, and **API Passphrase** — you will only see the secret once.
4. Your **Private Key** is the private key of the Polygon wallet you connected to Polymarket. Export it from MetaMask via **Account Details → Export Private Key**.

> **Security warning:** Never commit your `.env` file or share your private key.

---

## 3. Fund Your Polygon Wallet

- **USDC** — This is your trading collateral. Bridge USDC from Ethereum or another chain to Polygon using the [Polygon Bridge](https://wallet.polygon.technology/polygon/bridge) or buy directly on a Polygon-native DEX.
- **MATIC** — Required for gas fees on the Polygon network. You need a small amount (0.1–1 MATIC is usually sufficient for many transactions). Buy MATIC on any major exchange and withdraw to your Polygon address.

Polymarket uses USDC on Polygon as its settlement currency. Deposit USDC into Polymarket from your wallet on the Polymarket site before trading.

---

## 4. Configure `.env`

Create a `.env` file in the `polymarket-btc-bot/` directory:

```env
POLYMARKET_PRIVATE_KEY=0xYourPrivateKeyHere
POLYMARKET_API_KEY=your-api-key
POLYMARKET_API_SECRET=your-api-secret
POLYMARKET_API_PASSPHRASE=your-passphrase

# Bot settings
DRY_RUN=true
MAX_POSITION_SIZE=15
MAX_OPEN_POSITIONS=3
DAILY_LOSS_LIMIT=75
ENTRY_THRESHOLD=0.07
POLL_INTERVAL_SECONDS=300
FORCE_CLOSE_MINUTES_BEFORE_RESOLUTION=45
```

| Variable | Description |
|---|---|
| `DRY_RUN` | `true` = simulate orders without hitting the API |
| `MAX_POSITION_SIZE` | Max USDC per position |
| `MAX_OPEN_POSITIONS` | Max simultaneous open positions |
| `DAILY_LOSS_LIMIT` | Stop trading for the day if P&L drops below -$N |
| `ENTRY_THRESHOLD` | Minimum edge (fair value vs market price) to enter |
| `POLL_INTERVAL_SECONDS` | Seconds between each loop iteration |
| `FORCE_CLOSE_MINUTES_BEFORE_RESOLUTION` | Minutes before resolution to force-close all positions |

---

## 5. Run in Dry-Run Mode First

With `DRY_RUN=true` in your `.env`, the bot will log all signals and simulated orders without placing real trades:

```bash
python main.py
```

You will see a status table every 5 minutes (or whatever `POLL_INTERVAL_SECONDS` is set to) showing BTC price, open positions, and P&L.

Logs are written to `logs/bot.log` and rotate at 10 MB.

---

## 6. Use the Backtest Flag

Run the strategy against the last 7 days of BTC price data (5-minute candles from Binance) to evaluate performance before going live:

```bash
python main.py --backtest
```

Output example:

```
=======================================================
  BACKTEST RESULTS (7 days, 5-min candles)
=======================================================
  Total trades      : 42
  Winners           : 28 (66.7%)
  Losers            : 14
  Gross P&L         : $87.34
  Avg win           : $5.12
  Avg loss          : $-3.20
  Daily vol used    : 0.0234
  Exits [take_profit]: 28
  Exits [stop_loss] : 10
  Exits [force_close]: 4
=======================================================
```

> Note: the backtest uses a simplified market price of 0.50 as a proxy for all markets. Real performance will vary based on actual market liquidity and bid/ask spreads.

---

## 7. Go Live

Once you are satisfied with dry-run and backtest results:

1. Set `DRY_RUN=false` in `.env`
2. Ensure your Polymarket account has USDC deposited
3. Start the bot: `python main.py`

---

## Strategy Overview

- **Fair value** is computed using a sigmoid model based on distance from strike, time remaining, and realized volatility.
- **Entry**: trades when `|fair_value - market_price| > ENTRY_THRESHOLD`, skips if BTC is within 0.5% of strike, or if fewer than 45 minutes remain.
- **Exit**: take profit at +4%, stop loss at -6%, force-close 45 minutes before resolution.
- **Risk controls**: max 3 simultaneous positions, $75/day loss limit, $15 max per position.

---

## File Overview

| File | Purpose |
|---|---|
| `main.py` | Main loop and backtest runner |
| `strategy.py` | Fair value formula, entry/exit logic |
| `polymarket.py` | Polymarket CLOB API wrapper |
| `price_feed.py` | Binance BTC price + realized volatility |
| `database.py` | SQLite persistence for positions and daily summaries |
| `risk.py` | Position sizing and risk gate checks |
| `logger.py` | Loguru console + rotating file logging |
| `config.py` | `.env` loading and validation |

---

## Disclaimer

This bot is for educational purposes. Trading prediction markets involves real financial risk. Past performance in backtests does not guarantee future results. Never trade with money you cannot afford to lose.
