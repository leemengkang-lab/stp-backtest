# ARS Bot — Asia-Range Sweep-Reversal (OANDA Practice)

Forward demo test of the ARS strategy validated in `stp-backtest`. Paper money only.
Lightweight by design for an e2-micro: one API cycle per 15 minutes, no pandas.

## What it trades
EUR_USD, AUD_USD, USD_JPY (GBP_USD dropped — no gross edge in backtest).
Marks the Asia range (00:00–06:00 UTC), then during London (07:00–13:00 UTC,
= 15:00–21:00 SGT) waits for a sweep beyond a range extreme and enters on the
first M15 close back inside, within 4 bars of the sweep. Stop beyond the sweep
wick + 0.3×ATR; target the opposite side of the range (min 1.2R, capped 3R).
One setup per pair per day, max 2 concurrent, 0.5% risk, BE after +1R,
circuit breakers at −2R/day and −4R/week (persist across restarts).

**Why stop buffer is 0.3 (backtest default was 0.1):** cost analysis showed
spread+slippage consumed 0.11–0.15R per trade on wick-tight stops — the entire
edge. A wider buffer dilutes the fixed pip cost as a fraction of R. This value
was NOT walk-forward validated (VM constraints); the demo run itself is the test.

## Safety rails
- News guard: pulls the Forex Factory weekly high-impact calendar; vetoes
  entries ±30 min around events (±60 for FOMC/NFP/CPI/rate decisions).
  **Fail-safe: if the calendar can't be fetched, no new entries are taken.**
- Spread guard: skips entry if live spread exceeds the per-pair cap,
  and logs the spread of every entry taken (this doubles as our real
  spread measurement).
- `touch HALT` in the bot directory → no new entries, management continues.
- `touch FLATTEN` → close everything, halt entries. Remove the files to resume.

## Deploy
```bash
mkdir ~/ars-bot && cd ~/ars-bot     # copy the .py files here
. ~/.venv/bin/activate && pip install requests

# add your account id (token already lives in /etc/forex-scalper.env)
echo 'OANDA_ACCOUNT_ID=your-practice-account-id' | sudo tee -a /etc/forex-scalper.env

# smoke test in foreground first (one cycle every 15 min; Ctrl-C to stop)
export OANDA_API_TOKEN="$(sudo grep -oP '(?<=^OANDA_TOKEN=).*' /etc/forex-scalper.env)"
export OANDA_ACCOUNT_ID="$(sudo grep -oP '(?<=^OANDA_ACCOUNT_ID=).*' /etc/forex-scalper.env)"
python ars_bot.py

# then install as a service
sudo cp ars-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/ars-bot.service   # check paths/user match your VM
sudo systemctl daemon-reload && sudo systemctl enable --now ars-bot
journalctl -u ars-bot -f
```
Note: the service file assumes your env file defines OANDA_TOKEN and
OANDA_ACCOUNT_ID; adjust the Environment lines if your naming differs.
IMPORTANT: use a SEPARATE practice sub-account from your old scalper bot so
the P&L is attributable to this strategy alone (create one in the OANDA portal).

## Outputs
- `ars_bot.log` — every decision: signals, vetoes (news/spread), entries, BE moves, closes.
- `ars_trades.csv` — one row per closed trade with r_mult, mfe_r, mae_r,
  spread_pips_at_entry. Fields align with diagnose.py's expectations.
- `ars_state.json` — circuit-breaker state; delete only if you want a hard reset.

## How to judge the run (decide these gates NOW, not after seeing results)
- Minimum sample: ~60–80 trades (~2.5–3 months) before any conclusion.
- Compare against backtest gross-edge expectations: the strategy is doing its
  job if expectancy lands around −0.05R to +0.10R at this sample size; the
  real information is in spread_pips_at_entry (actual costs), the news-veto
  count, and MFE/MAE shape vs backtest.
- Hard fail: expectancy below −0.15R after 80+ trades, or drawdown beyond
  −10R — halt and return to analysis.
- Do NOT touch parameters mid-run. A forward test with moving parameters
  measures nothing. If something must change, restart the trade count.
