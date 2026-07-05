"""
Backtest engine.

Design decisions (all conservative on purpose):
- Entry: market at NEXT M15 open after signal bar closes, paying half-spread.
- Exit fills pay half-spread; stop exits also pay slippage.
- If a bar's range touches BOTH stop and target, the STOP is assumed hit first.
- Breakeven move only allowed after the bar has *closed* with >= +1R open profit
  (checking intrabar highs for the BE trigger would be optimistic).
- Portfolio-level: max concurrent positions + daily/weekly R circuit breakers.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config


@dataclass
class Trade:
    pair: str
    direction: int          # +1 long, -1 short
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    risk_dist: float        # initial per-unit risk (price units)
    units: float
    exit_time: pd.Timestamp = None
    exit: float = None
    reason: str = ""
    r_mult: float = 0.0
    pnl: float = 0.0
    bars_held: int = 0
    be_moved: bool = False
    mfe_r: float = 0.0      # max favorable excursion in R (how far it went our way)
    mae_r: float = 0.0      # max adverse excursion in R (how far against us, <= 0)


@dataclass
class Portfolio:
    equity: float
    trades: list = field(default_factory=list)
    open_pos: dict = field(default_factory=dict)     # pair -> Trade
    day_r: float = 0.0
    week_r: float = 0.0
    cur_day: object = None
    cur_week: object = None
    equity_curve: list = field(default_factory=list)


def _roll_period(pf: Portfolio, ts: pd.Timestamp):
    d, w = ts.date(), ts.isocalendar()[:2]
    if d != pf.cur_day:
        pf.cur_day, pf.day_r = d, 0.0
    if w != pf.cur_week:
        pf.cur_week, pf.week_r = w, 0.0


def _close(pf: Portfolio, t: Trade, ts, price, reason, spec, p):
    half = spec["spread_pips"] * spec["pip"] / 2
    slip = spec["slippage_pips"] * spec["pip"] if reason == "stop" else 0.0
    fill = price - t.direction * (half + slip) if reason != "target" else price - t.direction * half
    t.exit_time, t.exit, t.reason = ts, fill, reason
    t.r_mult = t.direction * (fill - t.entry) / t.risk_dist
    t.pnl = t.direction * (fill - t.entry) * t.units
    pf.equity += t.pnl
    pf.day_r += t.r_mult
    pf.week_r += t.r_mult
    pf.trades.append(t)
    del pf.open_pos[t.pair]


def run_backtest(datasets: dict, params: dict,
                 start_equity: float | None = None) -> tuple[list, pd.Series]:
    """
    datasets: pair -> DataFrame from strategy.generate_signals().
    Numpy event loop: all pairs aligned on the union M15 index, integer-indexed.
    """
    p = params
    pf = Portfolio(equity=start_equity or config.ACCOUNT["starting_equity"])

    union = sorted(set().union(*[set(df.index) for df in datasets.values()]))
    union = pd.DatetimeIndex(union)
    n = len(union)

    # Pre-extract aligned numpy arrays per pair (NaN where the pair has no bar)
    arr = {}
    for pair, df in datasets.items():
        a = df.reindex(union)
        arr[pair] = {
            "open": a["open"].to_numpy(), "high": a["high"].to_numpy(),
            "low": a["low"].to_numpy(), "close": a["close"].to_numpy(),
            "signal": a["signal"].fillna(0).to_numpy(),
            "sl_dist": a["sl_dist"].to_numpy(), "tp_dist": a["tp_dist"].to_numpy(),
        }
    days = union.date
    weeks = pd.Index(union.isocalendar().week).to_numpy() + union.year.to_numpy() * 100
    pairs = list(datasets)
    specs = {pr: config.PAIR_SPECS[pr] for pr in pairs}

    pending = {}
    eq_out = np.empty(n)

    for i in range(n):
        # roll daily/weekly R counters
        if days[i] != pf.cur_day:
            pf.cur_day, pf.day_r = days[i], 0.0
        if weeks[i] != pf.cur_week:
            pf.cur_week, pf.week_r = weeks[i], 0.0

        for pair in pairs:
            a = arr[pair]
            o = a["open"][i]
            if o != o:          # NaN -> no bar for this pair at this timestamp
                continue
            hi, lo, cl = a["high"][i], a["low"][i], a["close"][i]
            spec = specs[pair]

            # ---- manage open position ----
            if pair in pf.open_pos:
                t = pf.open_pos[pair]
                t.bars_held += 1
                # track excursions (favorable/adverse) in R, including this bar
                fav = t.direction * ((hi if t.direction == 1 else lo) - t.entry) / t.risk_dist
                adv = t.direction * ((lo if t.direction == 1 else hi) - t.entry) / t.risk_dist
                if fav > t.mfe_r:
                    t.mfe_r = fav
                if adv < t.mae_r:
                    t.mae_r = adv
                hit_stop = (lo <= t.stop) if t.direction == 1 else (hi >= t.stop)
                hit_tp = (hi >= t.target) if t.direction == 1 else (lo <= t.target)
                if hit_stop:                      # pessimistic: stop before target
                    _close(pf, t, union[i], t.stop, "stop", spec, p)
                elif hit_tp:
                    _close(pf, t, union[i], t.target, "target", spec, p)
                elif p.get("time_stop_bars") and t.bars_held >= p["time_stop_bars"]:
                    _close(pf, t, union[i], cl, "time", spec, p)
                elif not t.be_moved:
                    open_r = t.direction * (cl - t.entry) / t.risk_dist
                    if open_r >= p["be_trigger_r"]:
                        t.stop = t.entry + t.direction * p["be_offset_pips"] * spec["pip"]
                        t.be_moved = True

            # ---- execute pending entry at this bar's open ----
            if pair in pending:
                sig = pending.pop(pair)
                if (pair not in pf.open_pos
                        and len(pf.open_pos) < p["max_open_positions"]
                        and pf.day_r > p["daily_stop_r"]
                        and pf.week_r > p["weekly_stop_r"]):
                    half = spec["spread_pips"] * spec["pip"] / 2
                    entry = o + sig["dir"] * half
                    risk_dist = sig["sl_dist"]
                    units = (pf.equity * p["risk_per_trade"]) / risk_dist
                    pf.open_pos[pair] = Trade(
                        pair=pair, direction=sig["dir"], entry_time=union[i],
                        entry=entry, stop=entry - sig["dir"] * risk_dist,
                        target=entry + sig["dir"] * sig["tp_dist"],
                        risk_dist=risk_dist, units=units,
                    )

            # ---- queue new signal from this closed bar ----
            s = a["signal"][i]
            if s != 0 and pair not in pf.open_pos:
                pending[pair] = {"dir": int(s), "sl_dist": a["sl_dist"][i],
                                 "tp_dist": a["tp_dist"][i]}

        eq_out[i] = pf.equity

    for pair in list(pf.open_pos):
        a = arr[pair]
        j = n - 1
        while a["close"][j] != a["close"][j]:
            j -= 1
        _close(pf, pf.open_pos[pair], union[j], a["close"][j], "eod",
               specs[pair], p)

    return pf.trades, pd.Series(eq_out, index=union)


# ---------- statistics ----------

def stats(trades: list, curve: pd.Series) -> dict:
    if not trades:
        return {"n_trades": 0}
    r = np.array([t.r_mult for t in trades])
    wins = r[r > 0]
    eq = curve.to_numpy()
    dd = (eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)
    months = max((curve.index[-1] - curve.index[0]).days / 30.44, 1e-9)
    return {
        "n_trades": len(r),
        "win_rate": round(float((r > 0).mean()), 3),
        "avg_win_r": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(r[r <= 0].mean()), 2) if (r <= 0).any() else 0.0,
        "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(float(wins.sum() / max(-r[r <= 0].sum(), 1e-9)), 2),
        "total_return_pct": round(float(eq[-1] / eq[0] - 1) * 100, 2),
        "max_drawdown_pct": round(float(dd.min()) * 100, 2),
        "trades_per_month": round(len(r) / months, 1),
        "median_bars_held": int(np.median([t.bars_held for t in trades])),
        "exit_breakdown": pd.Series([t.reason for t in trades]).value_counts().to_dict(),
    }
