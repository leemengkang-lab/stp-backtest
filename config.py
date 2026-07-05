"""
STP Backtest - configuration.
All tunable parameters live here. Nothing is hard-coded elsewhere.
Times are UTC internally. (SGT = UTC+8: Tokyo window 08-12 SGT = 00-04 UTC,
London window 15-21 SGT = 07-13 UTC.)
"""

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]

# Which pairs may trade in which session (UTC hours, [start, end) )
SESSIONS = {
    "tokyo":  {"hours": (0, 4),  "pairs": ["USD_JPY", "AUD_USD"]},
    "london": {"hours": (7, 13), "pairs": ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]},
}

# Strategy parameters (defaults; walk-forward optimizes the ones in WF_GRID)
STRATEGY = {
    "ema_fast": 20,
    "ema_slow": 50,
    "atr_period": 14,
    "rsi_period": 14,
    "rsi_long_level": 40,      # long trigger: RSI crosses UP through this
    "rsi_short_level": 60,     # short trigger: RSI crosses DOWN through this (100 - long level)
    "sl_atr_mult": 1.5,        # stop  = 1.5 x H1 ATR
    "tp_atr_mult": 2.5,        # target = 2.5 x H1 ATR
    "atr_min_ratio": 0.7,      # skip if H1 ATR < 0.7 x SMA50(ATR)
    "pullback_atr_dist": 0.5,  # entry must be within 0.5 x ATR of H1 EMA20
    "be_trigger_r": 1.0,       # move stop to breakeven only after +1R
    "be_offset_pips": 1.0,
    "max_open_positions": 2,
    "risk_per_trade": 0.005,   # 0.5% of equity
    "daily_stop_r": -2.0,      # circuit breaker: halt day after -2R
    "weekly_stop_r": -4.0,     # halt week after -4R
    "time_stop_bars": None,    # safety net; set after measuring median time-to-target
}

# Per-pair market microstructure (pips). Adjust to your OANDA live stats.
PAIR_SPECS = {
    "EUR_USD": {"pip": 0.0001, "spread_pips": 0.9, "slippage_pips": 0.3, "min_sl_pips": 10},
    "GBP_USD": {"pip": 0.0001, "spread_pips": 1.3, "slippage_pips": 0.4, "min_sl_pips": 10},
    "USD_JPY": {"pip": 0.01,   "spread_pips": 1.0, "slippage_pips": 0.3, "min_sl_pips": 15},
    "AUD_USD": {"pip": 0.0001, "spread_pips": 1.1, "slippage_pips": 0.3, "min_sl_pips": 10},
}

ACCOUNT = {"starting_equity": 100_000.0}

# News blackout. Provide a CSV: timestamp_utc,currency,impact  (impact: high/medium/low)
NEWS = {
    "csv_path": "news_calendar.csv",   # optional; warning if missing
    "blackout_before_min": 30,
    "blackout_after_min": 30,
    "tier1_events_extra_min": 30,      # extra padding either side for FOMC/NFP-class rows
    "tier1_keywords": ["FOMC", "Non-Farm", "NFP", "Rate Decision", "CPI"],
    "currency_map": {                  # which event currencies affect which pair
        "EUR_USD": ["EUR", "USD"], "GBP_USD": ["GBP", "USD"],
        "USD_JPY": ["USD", "JPY"], "AUD_USD": ["AUD", "USD", "CNY"],
    },
}

# Walk-forward settings
WALKFORWARD = {
    "train_months": 6,
    "test_months": 2,
    "grid": {                          # small grid on purpose - big grids curve-fit
        "sl_atr_mult": [1.25, 1.5, 1.75],
        "tp_atr_mult": [2.0, 2.5, 3.0],
        "rsi_long_level": [40, 45],
    },
    "objective": "expectancy_r",       # metric maximized on train windows
    "min_train_trades": 30,            # window invalid if fewer trades than this
}

# Walk-forward grid for strategy V2 (Asia-range sweep-reversal)
WALKFORWARD_V2 = {
    "grid": {
        "min_rr": [1.0, 1.2, 1.5],
        "stop_buffer_atr": [0.05, 0.15],
        "require_h4_align": [False, True],
    },
}

MONTE_CARLO = {"n_shuffles": 1000, "seed": 42}

# OANDA data settings (demo/practice endpoint)
OANDA = {
    "env_token": "OANDA_API_TOKEN",    # read from environment variable
    "granularity": "M15",
    "price": "M",                      # mid candles; costs modeled separately
    "api_url": "https://api-fxpractice.oanda.com",
    "cache_dir": "data_cache",
}
