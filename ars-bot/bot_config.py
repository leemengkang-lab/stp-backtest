"""
ARS live bot — configuration.
Asia-Range Sweep-Reversal, OANDA practice account, forward demo test.
All times UTC (SGT = UTC+8).
"""

PAIRS = ["EUR_USD", "AUD_USD", "USD_JPY"]   # GBP_USD dropped: no gross edge in backtest

STRATEGY = {
    "asia_hours": (0, 6),          # Asia range built from 00:00-05:45 UTC bars
    "entry_hours": (7, 13),        # entries only 07:00-12:45 UTC (15:00-20:45 SGT)
    "sweep_buffer_atr": 0.05,      # pierce must exceed extreme by this x H1 ATR
    "stop_buffer_atr": 0.30,       # stop beyond sweep wick (0.30: dilutes fixed pip
                                   #   costs vs backtest's 0.10 — see README rationale)
    "reclaim_within_bars": 4,      # sweep must be reclaimed within 4 M15 bars
    "min_rr": 1.2,                 # skip setups where range target < 1.2R
    "max_rr": 3.0,                 # cap target at 3R
    "range_min_atr": 0.8,          # Asia range sanity band vs H1 ATR
    "range_max_atr": 3.5,
    "require_h4_align": False,
    "be_trigger_r": 1.0,           # move stop to BE only after a bar closes >= +1R
    "be_offset_pips": 1.0,
    "risk_per_trade": 0.005,       # 0.5% of NAV
    "max_open_positions": 2,
    "daily_stop_r": -2.0,          # circuit breakers (persisted across restarts)
    "weekly_stop_r": -4.0,
}

PAIR_SPECS = {
    "EUR_USD": {"pip": 0.0001, "precision": 5, "min_sl_pips": 10, "max_spread_pips": 1.5},
    "AUD_USD": {"pip": 0.0001, "precision": 5, "min_sl_pips": 10, "max_spread_pips": 1.8},
    "USD_JPY": {"pip": 0.01,   "precision": 3, "min_sl_pips": 15, "max_spread_pips": 1.6},
}

NEWS = {
    # Forex Factory weekly calendar (free JSON, no key). Refetched every 6 hours.
    "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "blackout_before_min": 30,
    "blackout_after_min": 30,
    "tier1_extra_min": 30,         # extra padding for FOMC/NFP/CPI/rate decisions
    "tier1_keywords": ["fomc", "non-farm", "nonfarm", "cpi", "rate"],
    "currency_map": {
        "EUR_USD": ["EUR", "USD"], "AUD_USD": ["AUD", "USD", "CNY"],
        "USD_JPY": ["USD", "JPY"],
    },
    # If the calendar cannot be fetched, take no NEW entries (fail-safe).
    "halt_entries_without_calendar": True,
}

OANDA = {
    "api_url": "https://api-fxpractice.oanda.com",
    "env_token": "OANDA_API_TOKEN",
    "env_account": "OANDA_ACCOUNT_ID",
}

FILES = {
    "state": "ars_state.json",     # circuit breakers, traded-today, tracked trades
    "journal": "ars_trades.csv",   # one row per closed trade (diagnose-compatible fields)
    "log": "ars_bot.log",
    "halt_flag": "HALT",           # touch this file -> no new entries
    "flatten_flag": "FLATTEN",     # touch this file -> close everything and halt
}
