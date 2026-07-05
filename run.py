"""
STP backtest runner.

Usage (on your VM, with OANDA_API_TOKEN set):
    python run.py --start 2023-07-01 --end 2026-06-30
    python run.py --start 2023-07-01 --end 2026-06-30 --walkforward

Smoke test without market data (mechanics only, results meaningless):
    python run.py --start 2024-01-01 --end 2025-06-30 --source synthetic --walkforward
"""
import argparse
import json

import config
import data
import strategy
import engine
import walkforward as wf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--source", default="oanda", choices=["oanda", "synthetic"])
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--montecarlo", action="store_true")
    args = ap.parse_args()

    if args.source == "synthetic":
        print("=" * 66)
        print("SYNTHETIC DATA MODE - pipeline verification only.")
        print("Do NOT interpret these numbers as strategy performance.")
        print("=" * 66)

    print(f"\nLoading {len(config.PAIRS)} pairs, {args.start} -> {args.end} ...")
    raw = {p: data.get_data(p, args.start, args.end, args.source) for p in config.PAIRS}
    for p, df in raw.items():
        print(f"  {p}: {len(df):,} M15 bars")
    news = data.load_news()

    # ---- precompute indicator features once ----
    feats, blackouts = wf.precompute(raw, news)

    # ---- single full-period backtest with default params ----
    params = dict(config.STRATEGY)
    datasets = {p: strategy.generate_signals(feats[p], p, params, blackouts[p])
                for p in feats}

    trades, curve = engine.run_backtest(datasets, params)
    print("\n--- Full-period backtest (default parameters) ---")
    print(json.dumps(engine.stats(trades, curve), indent=2, default=str))

    per_pair = {}
    for pr in config.PAIRS:
        pt = [t for t in trades if t.pair == pr]
        if pt:
            import numpy as np
            r = np.array([t.r_mult for t in pt])
            per_pair[pr] = {"n": len(pt), "expectancy_r": round(float(r.mean()), 3)}
    print("\nPer-pair expectancy:")
    print(json.dumps(per_pair, indent=2))

    if args.walkforward:
        print("\n--- Walk-forward analysis ---")
        res = wf.walk_forward(feats, blackouts)
        for w in res["windows"]:
            print(json.dumps(w, default=str))
        print("\nStitched OUT-OF-SAMPLE summary (the number that matters):")
        print(json.dumps(res["oos_summary"], indent=2))
        mc_source = res["oos_trades"] or trades
    else:
        mc_source = trades

    if args.montecarlo or args.walkforward:
        print("\n--- Monte Carlo (1000 shuffles) ---")
        print(json.dumps(wf.monte_carlo(mc_source), indent=2))

    # export trade log for inspection
    if trades:
        import pandas as pd
        pd.DataFrame([vars(t) for t in trades]).to_csv("trade_log.csv", index=False)
        print("\nTrade log written to trade_log.csv")


if __name__ == "__main__":
    main()
