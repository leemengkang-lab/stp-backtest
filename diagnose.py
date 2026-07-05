"""
diagnose.py - post-hoc diagnosis of a trade_log.csv produced by engine.py.

Answers the three questions that decide the next structural move:
  1. Do losers carry information?   -> MFE of LOSING trades
  2. Is the target too greedy?      -> MFE-based TP sweep
  3. Does the edge live in a subset?-> segment breakdown by pair / side / session

Usage:  python diagnose.py trade_log.csv

IMPORTANT: the TP sweep is an APPROXIMATION from recorded MFE, holding the stop
and breakeven-after-1R logic fixed. If a target level looks better here, CONFIRM
it by re-running the real backtest at that tp_atr_mult - never trust the
approximation as proof (that is the whole "diagnose, then re-validate" point).
"""
import sys

import numpy as np
import pandas as pd

import config


def _session(hour: int) -> str:
    for name, s in config.SESSIONS.items():
        lo, hi = s["hours"]
        if lo <= hour < hi:
            return name
    return "other"


def _exp(df: pd.DataFrame) -> float:
    return round(float(df["r_mult"].mean()), 3) if len(df) else 0.0


def _quantiles(series, qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict:
    return {f"p{int(q * 100)}": round(float(series.quantile(q)), 2) for q in qs}


def tp_sweep(df: pd.DataFrame, be_trigger: float) -> pd.DataFrame:
    """Counterfactual expectancy at various targets (in R), estimated from MFE.

    Model per trade: if mfe_r >= t -> +t (target hit); elif it reached breakeven
    (mfe_r >= be_trigger) -> ~0R; else -> -1R (stopped). Costs held as realized.
    """
    rows = []
    for t in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0]:
        out = np.where(df["mfe_r"] >= t, t,
                       np.where(df["mfe_r"] >= be_trigger, 0.0, -1.0))
        rows.append({
            "target_R": t,
            "expectancy_R": round(float(out.mean()), 3),
            "win_rate": round(float((out > 0).mean()), 3),
            "hit_target_pct": round(float((df["mfe_r"] >= t).mean()) * 100, 1),
        })
    return pd.DataFrame(rows)


def segment(df: pd.DataFrame, col: str) -> pd.DataFrame:
    out = (df.groupby(col)["r_mult"].agg(["count", "mean"])
             .rename(columns={"mean": "expectancy_R"}))
    out["expectancy_R"] = out["expectancy_R"].round(3)
    return out.sort_values("expectancy_R", ascending=False)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "trade_log.csv"
    df = pd.read_csv(path)
    if "mfe_r" not in df.columns:
        print("ERROR: this trade_log.csv has no mfe_r column. Pull the updated engine.py "
              "(which logs MFE/MAE) and RE-RUN the backtest before diagnosing.")
        sys.exit(1)
    if df.empty:
        print("No trades in the log.")
        sys.exit(0)

    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["session"] = df["entry_time"].dt.hour.map(_session)
    df["side"] = np.where(df["direction"] == 1, "long", "short")
    n = len(df)

    print(f"\n=== {n} trades | overall expectancy {_exp(df)}R | "
          f"win rate {round(float((df['r_mult'] > 0).mean()), 3)} ===")

    losers = df[df["r_mult"] <= 0]
    winners = df[df["r_mult"] > 0]

    print("\n--- 1) Do losers carry information? (MFE of LOSING trades, in R) ---")
    if len(losers):
        print("  loser MFE quantiles:", _quantiles(losers["mfe_r"]))
        med = float(losers["mfe_r"].median())
        reached_1r = float((losers["mfe_r"] >= 1.0).mean()) * 100
        print(f"  median loser MFE: {round(med, 2)}R  |  "
              f"{round(reached_1r, 1)}% of losers reached +1R before dying")
        if med < 0.3:
            hint = ("BRANCH 1 - losers barely move in your favor: the ENTRY carries no edge. "
                    "Redesign entries; do NOT touch exits.")
        elif med >= 0.8:
            hint = ("BRANCH 2 - losers travel far before dying: entries are fine, the target "
                    "is too greedy. See the TP sweep below for where it turns positive.")
        else:
            hint = ("AMBIGUOUS (0.3-0.8R): weak entry signal. Check the segment tables for a "
                    "subset that works before redesigning.")
        print("  >>", hint)

    print("\n--- MAE of WINNING trades (heat winners survive, in R) ---")
    if len(winners):
        print("  winner MAE quantiles:", _quantiles(winners["mae_r"]))

    print("\n--- 2) TP sweep (APPROX from MFE; confirm any change by re-running) ---")
    be = float(config.STRATEGY.get("be_trigger_r", 1.0))
    print(tp_sweep(df, be).to_string(index=False))

    print("\n--- 3) Segment breakdown: by PAIR ---")
    print(segment(df, "pair").to_string())
    print("\n    by SIDE ---")
    print(segment(df, "side").to_string())
    print("\n    by SESSION ---")
    print(segment(df, "session").to_string())

    seg = segment(df, "pair")
    if _exp(df) <= 0 and (seg["expectancy_R"] > 0.1).any():
        good = seg[seg["expectancy_R"] > 0.1].index.tolist()
        print(f"\n  >> BRANCH 3 - overall negative but positive subset(s): {good}. "
              f"Consider narrowing the system to these before tuning anything.")

    print("\n--- exit reason mix ---")
    print(df["reason"].value_counts().to_string())


if __name__ == "__main__":
    main()
