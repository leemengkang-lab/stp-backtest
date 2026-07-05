"""
Walk-forward optimization + Monte Carlo trade-shuffle analysis.

Walk-forward: optimize the small grid on each train window (default 6 months),
then run the winning parameters on the following unseen test window (2 months),
roll forward, and stitch the test-window trades together. The stitched
out-of-sample record is the only performance number that matters.

Performance note: indicator features (EMA/ATR/RSI, HTF mapping) do not depend on
the grid parameters (SL/TP multiples, RSI trigger level), so they are computed
ONCE per pair and reused across every grid combination and window.

Interpretation guide:
- OOS expectancy >= ~60% of in-sample expectancy  -> parameters are stable. Good.
- OOS collapses to ~0 or negative                 -> the edge was curve-fit. Stop.
- Winning parameters jump around wildly per window -> same conclusion.
"""
import itertools

import numpy as np
import pandas as pd

import config
import strategy
import engine


def _windows(start: pd.Timestamp, end: pd.Timestamp):
    tr = pd.DateOffset(months=config.WALKFORWARD["train_months"])
    te = pd.DateOffset(months=config.WALKFORWARD["test_months"])
    cur = start
    while cur + tr + te <= end:
        yield (cur, cur + tr, cur + tr + te)   # train_start, split, test_end
        cur = cur + te


def precompute(raw: dict, news) -> tuple[dict, dict]:
    """Build indicator features + blackout masks once per pair."""
    base = dict(config.STRATEGY)
    feats, blackouts = {}, {}
    for pair, m15 in raw.items():
        f = strategy.build_features(m15, base)
        feats[pair] = f
        blackouts[pair] = strategy.build_news_blackout(news, pair, f.index)
    return feats, blackouts


def _run(feats: dict, blackouts: dict, params: dict, t0, t1) -> dict:
    datasets = {}
    for pair, f in feats.items():
        sl = f.loc[t0:t1]
        if len(sl) < 500:
            continue
        datasets[pair] = strategy.generate_signals(
            sl, pair, params, blackouts[pair].loc[t0:t1])
    if not datasets:
        return {"n_trades": 0}
    trades, curve = engine.run_backtest(datasets, params)
    s = engine.stats(trades, curve)
    s["_trades"] = trades
    return s


def walk_forward(feats: dict, blackouts: dict) -> dict:
    base = dict(config.STRATEGY)
    grid = config.WALKFORWARD["grid"]
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]
    obj = config.WALKFORWARD["objective"]

    start = max(f.index[0] for f in feats.values())
    end = min(f.index[-1] for f in feats.values())

    oos_trades, report = [], []
    for t0, split, t2 in _windows(start, end):
        best, best_score = None, -np.inf
        for c in combos:
            params = {**base, **c}
            s = _run(feats, blackouts, params, t0, split)
            if s.get("n_trades", 0) < config.WALKFORWARD["min_train_trades"]:
                continue
            if s[obj] > best_score:
                best_score, best = s[obj], c
        if best is None:
            report.append({"train": (str(t0.date()), str(split.date())),
                           "note": "insufficient trades"})
            continue
        s_test = _run(feats, blackouts, {**base, **best}, split, t2)
        oos_trades.extend(s_test.pop("_trades", []))
        report.append({
            "train": (str(t0.date()), str(split.date())),
            "test": (str(split.date()), str(t2.date())),
            "best_params": best,
            "train_" + obj: round(best_score, 3),
            "test_" + obj: s_test.get(obj, None),
            "test_trades": s_test.get("n_trades", 0),
        })

    # stitched OOS equity in R-space
    if oos_trades:
        oos_trades.sort(key=lambda t: t.entry_time)
        r = np.array([t.r_mult for t in oos_trades])
        eq = 1 + np.cumsum(r) * config.STRATEGY["risk_per_trade"]
        dd = (eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)
        oos = {
            "n_trades": len(r),
            "expectancy_r": round(float(r.mean()), 3),
            "win_rate": round(float((r > 0).mean()), 3),
            "approx_return_pct": round(float(eq[-1] - 1) * 100, 2),
            "approx_max_dd_pct": round(float(dd.min()) * 100, 2),
        }
    else:
        oos = {"n_trades": 0}
    return {"windows": report, "oos_summary": oos, "oos_trades": oos_trades}


def monte_carlo(trades: list) -> dict:
    """Shuffle the trade sequence to estimate the drawdown distribution."""
    if len(trades) < 20:
        return {"note": "too few trades for Monte Carlo"}
    rng = np.random.default_rng(config.MONTE_CARLO["seed"])
    r = np.array([t.r_mult for t in trades])
    risk = config.STRATEGY["risk_per_trade"]
    dds, finals = [], []
    for _ in range(config.MONTE_CARLO["n_shuffles"]):
        sh = rng.permutation(r)
        eq = 1 + np.cumsum(sh) * risk
        peak = np.maximum.accumulate(eq)
        dds.append(((eq - peak) / peak).min())
        finals.append(eq[-1] - 1)
    dds, finals = np.array(dds) * 100, np.array(finals) * 100
    return {
        "median_max_dd_pct": round(float(np.median(dds)), 2),
        "p95_max_dd_pct": round(float(np.percentile(dds, 5)), 2),   # worse tail
        "worst_max_dd_pct": round(float(dds.min()), 2),
        "median_return_pct": round(float(np.median(finals)), 2),
        "prob_negative_pct": round(float((finals < 0).mean()) * 100, 1),
        "note": "set weekly/monthly circuit breakers using p95 drawdown x safety margin",
    }
