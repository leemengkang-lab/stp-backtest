# STP Backtest - Session Trend-Pullback Strategy Validator

Standalone walk-forward backtester for the STP strategy, run against OANDA
practice-API historical M15 candles. Separate from the live `forex-scalper` bot:
this is the offline validator that must show a real edge before STP is wired into
the bot for a demo soak.

## Files
- `config.py` - every parameter (strategy, costs, sessions, walk-forward grid, news)
- `data.py` - OANDA paginated M15 fetch with CSV cache; synthetic generator for smoke tests
- `strategy.py` - indicators + lookahead-safe multi-timeframe signals
- `engine.py` - bar-by-bar portfolio sim (spread/slippage, breakeven-after-1R, circuit breakers)
- `walkforward.py` - rolling optimize(6mo)/test(2mo) walk-forward + Monte Carlo shuffle
- `run.py` - CLI entry point

## Setup on the VM
```bash
git clone <this-repo-url> ~/stp_backtest && cd ~/stp_backtest
python3 -m venv .venv && . .venv/bin/activate
pip install pandas numpy requests
export OANDA_API_TOKEN="your-practice-api-token"   # same token the bot uses
```

## Usage
```bash
# 3-year real run with walk-forward + Monte Carlo (first run downloads + caches candles)
python run.py --start 2023-07-01 --end 2026-06-30 --walkforward

# quick single backtest, default parameters
python run.py --start 2025-01-01 --end 2026-06-30

# pipeline smoke test, no market data / token needed (results meaningless by design)
python run.py --start 2024-01-01 --end 2025-06-30 --source synthetic --walkforward
```

## Decision gates (how to read the output)
1. Full-period `expectancy_r > 0` on real data, per pair - pairs that fail get dropped.
2. Stitched OOS expectancy >= ~60% of train expectancy and positive; else it's curve-fit - stop.
3. Monte Carlo p95 max drawdown x 2 must be tolerable - use it to set live circuit breakers.
4. Only then: 3 months demo, comparing demo win rate (+/-10%) and avg R (+/-20%) vs backtest.

## Sanity check
On random synthetic data the system loses ~ transaction costs (near-zero/negative
expectancy). That is the proof there is no lookahead leakage - a leaky backtest
would show a fake profit on noise.

## News blackout
Optional `news_calendar.csv` in the working dir: `timestamp_utc,currency,impact,event`
(impact = high/medium/low). Without it the backtest runs with NO news filter and
warns you - live results will then differ, so source a historical high-impact
calendar before trusting the numbers.
