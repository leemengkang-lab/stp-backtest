"""
Strategy V2 - Asia-Range Sweep-Reversal (ARS).

Thesis: London's opening flows frequently run the Asia session's high or low
to harvest resting stops, then reverse into the day's true direction. Instead
of buying pullbacks INTO that flush (V1's fatal flaw), we wait for the flush
to complete and enter on the reclaim.

Rules per pair, per day (all UTC):
1. Asia range = high/low of bars in asia_hours (default 00:00-05:45).
2. During entry_hours (default 07:00-12:45), watch for a SWEEP:
   a bar trading beyond the range extreme by >= sweep_buffer_atr x H1 ATR.
3. RECLAIM: within reclaim_within_bars of the sweep, a bar CLOSES back inside
   the range. Sweep of the LOW + reclaim -> LONG. Sweep of HIGH + reclaim -> SHORT.
4. Stop: beyond the sweep extreme + stop_buffer_atr x ATR (min pip floor applies).
5. Target: the OPPOSITE side of the Asia range. Skip the trade if reward/risk
   < min_rr; cap the target at max_rr x risk if the range is huge.
6. Quality band: Asia range must be between range_min_atr and range_max_atr
   x H1 ATR (too small = noise day, too large = news already happened).
7. Max ONE signal per pair per day. Optional H4 trend alignment filter.

Same interface as strategy.generate_signals(): takes the feature frame from
strategy.build_features() (needs OHLC + h1_atr + h4 columns), returns a frame
with signal / sl_dist / tp_dist columns for engine.run_backtest().
"""
import numpy as np
import pandas as pd

import config

DEFAULTS = {
    "asia_hours": (0, 6),
    "entry_hours": (7, 13),
    "sweep_buffer_atr": 0.05,
    "stop_buffer_atr": 0.10,
    "reclaim_within_bars": 4,
    "min_rr": 1.2,
    "max_rr": 3.0,
    "range_min_atr": 0.8,
    "range_max_atr": 3.5,
    "require_h4_align": False,
}


def generate_signals(f: pd.DataFrame, pair: str, params: dict,
                     blackout: pd.Series) -> pd.DataFrame:
    p = {**DEFAULTS, **params}
    spec = config.PAIR_SPECS[pair]
    min_sl = spec["min_sl_pips"] * spec["pip"]

    out = f.copy()
    out["signal"] = 0
    out["sl_dist"] = np.nan
    out["tp_dist"] = np.nan

    hours = f.index.hour
    dates = f.index.date
    high = f["high"].to_numpy(); low = f["low"].to_numpy()
    close = f["close"].to_numpy()
    atr = f["h1_atr"].to_numpy()
    h4_up = (f["h4_close"] > f["h4_ema_f"]) & (f["h4_ema_f"] > f["h4_ema_s"])
    h4_dn = (f["h4_close"] < f["h4_ema_f"]) & (f["h4_ema_f"] < f["h4_ema_s"])
    h4_up = h4_up.to_numpy(); h4_dn = h4_dn.to_numpy()
    blk = blackout.reindex(f.index, fill_value=False).to_numpy()

    sig = np.zeros(len(f)); sld = np.full(len(f), np.nan); tpd = np.full(len(f), np.nan)

    a_lo, a_hi = p["asia_hours"]; e_lo, e_hi = p["entry_hours"]
    pos = np.arange(len(f))

    for _, day_idx in pd.Series(pos, index=f.index).groupby(dates):
        d = day_idx.to_numpy()
        h = hours[d]
        asia = d[(h >= a_lo) & (h < a_hi)]
        entry_bars = d[(h >= e_lo) & (h < e_hi)]
        if len(asia) < 8 or len(entry_bars) == 0:
            continue
        asia_hi = high[asia].max(); asia_lo = low[asia].min()
        rng = asia_hi - asia_lo
        ref_atr = atr[entry_bars[0]]
        if not (p["range_min_atr"] * ref_atr <= rng <= p["range_max_atr"] * ref_atr):
            continue

        state, extreme, sweep_i = None, None, None
        for i in entry_bars:
            pierce = p["sweep_buffer_atr"] * atr[i]

            if state is None:
                if low[i] < asia_lo - pierce:
                    state, extreme, sweep_i = "low", low[i], i
                elif high[i] > asia_hi + pierce:
                    state, extreme, sweep_i = "high", high[i], i
                else:
                    continue  # no sweep yet

            if state == "low":
                extreme = min(extreme, low[i])
                if close[i] > asia_lo:                       # reclaim -> LONG
                    if blk[i] or (p["require_h4_align"] and not h4_up[i]):
                        break
                    stop_px = extreme - p["stop_buffer_atr"] * atr[i]
                    risk = max(close[i] - stop_px, min_sl)
                    reward = asia_hi - close[i]
                    if reward >= p["min_rr"] * risk:
                        sig[i] = 1; sld[i] = risk
                        tpd[i] = min(reward, p["max_rr"] * risk)
                    break                                    # one setup per day
                if i - sweep_i > p["reclaim_within_bars"]:
                    state = None                             # stale sweep: trend day

            elif state == "high":
                extreme = max(extreme, high[i])
                if close[i] < asia_hi:                       # reclaim -> SHORT
                    if blk[i] or (p["require_h4_align"] and not h4_dn[i]):
                        break
                    stop_px = extreme + p["stop_buffer_atr"] * atr[i]
                    risk = max(stop_px - close[i], min_sl)
                    reward = close[i] - asia_lo
                    if reward >= p["min_rr"] * risk:
                        sig[i] = -1; sld[i] = risk
                        tpd[i] = min(reward, p["max_rr"] * risk)
                    break
                if i - sweep_i > p["reclaim_within_bars"]:
                    state = None

    out["signal"] = sig.astype(int)
    out["sl_dist"] = sld
    out["tp_dist"] = tpd
    return out
