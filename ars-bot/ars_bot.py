"""
ARS live bot — Asia-Range Sweep-Reversal on OANDA practice.

Design: stateless recomputation. Every 15 minutes the bot re-derives today's
Asia range and sweep/reclaim state from scratch from today's M15 candles, so
restarts are harmless. Persistent state (circuit breakers, traded-today,
tracked trades) lives in ars_state.json.

Run:  python3 ars_bot.py            (or via systemd, see ars-bot.service)
Ops:  touch HALT     -> no new entries (management continues)
      touch FLATTEN  -> close all positions and halt entries
"""
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import bot_config as cfg
from oanda_client import OandaClient
from news_guard import NewsGuard

log = logging.getLogger("ars")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(cfg.FILES["log"]), logging.StreamHandler()],
)

P = cfg.STRATEGY


# ---------------- pure strategy logic (unit-testable) ----------------

def atr_from_h1(candles, n=14):
    """Wilder ATR via EWM alpha=1/n — same formula as the backtest."""
    atr = None
    prev_close = None
    for c in candles:
        tr = c["h"] - c["l"] if prev_close is None else max(
            c["h"] - c["l"], abs(c["h"] - prev_close), abs(c["l"] - prev_close))
        atr = tr if atr is None else atr + (tr - atr) / n
        prev_close = c["c"]
    return atr


def evaluate_day(bars, atr, pair):
    """Run the sweep/reclaim state machine over today's completed M15 bars.
    Returns a signal dict only if the RECLAIM happened on the LAST closed bar
    (so we act exactly once, at the same bar the backtest would have)."""
    spec = cfg.PAIR_SPECS[pair]
    a_lo, a_hi = P["asia_hours"]
    e_lo, e_hi = P["entry_hours"]

    def hr(b):
        return int(b["time"][11:13])

    asia = [b for b in bars if a_lo <= hr(b) < a_hi]
    if len(asia) < 8:
        return None
    asia_hi = max(b["h"] for b in asia)
    asia_lo = min(b["l"] for b in asia)
    rng = asia_hi - asia_lo
    if not (P["range_min_atr"] * atr <= rng <= P["range_max_atr"] * atr):
        return None

    entry_bars = [b for b in bars if e_lo <= hr(b) < e_hi]
    if not entry_bars:
        return None

    pierce = P["sweep_buffer_atr"] * atr
    state, extreme, sweep_i = None, None, None
    for i, b in enumerate(entry_bars):
        if state is None:
            if b["l"] < asia_lo - pierce:
                state, extreme, sweep_i = "low", b["l"], i
            elif b["h"] > asia_hi + pierce:
                state, extreme, sweep_i = "high", b["h"], i
            else:
                continue
        if state == "low":
            extreme = min(extreme, b["l"])
            if b["c"] > asia_lo:
                if i != len(entry_bars) - 1:
                    return None          # reclaim happened earlier; stale
                stop_px = extreme - P["stop_buffer_atr"] * atr
                risk = max(b["c"] - stop_px, spec["min_sl_pips"] * spec["pip"])
                reward = asia_hi - b["c"]
                if reward < P["min_rr"] * risk:
                    return None
                return {"dir": 1, "ref_close": b["c"], "risk": risk,
                        "reward": min(reward, P["max_rr"] * risk)}
            if i - sweep_i > P["reclaim_within_bars"]:
                state = None
        elif state == "high":
            extreme = max(extreme, b["h"])
            if b["c"] < asia_hi:
                if i != len(entry_bars) - 1:
                    return None
                stop_px = extreme + P["stop_buffer_atr"] * atr
                risk = max(stop_px - b["c"], spec["min_sl_pips"] * spec["pip"])
                reward = b["c"] - asia_lo
                if reward < P["min_rr"] * risk:
                    return None
                return {"dir": -1, "ref_close": b["c"], "risk": risk,
                        "reward": min(reward, P["max_rr"] * risk)}
            if i - sweep_i > P["reclaim_within_bars"]:
                state = None
    return None


def units_for_risk(pair, risk_usd, sl_dist, mid):
    """Position size so that sl_dist of adverse move loses ~risk_usd (USD account)."""
    if pair.endswith("_USD"):
        return risk_usd / sl_dist                # quote is USD
    if pair == "USD_JPY":
        return risk_usd * mid / sl_dist          # P&L in JPY -> convert at mid
    raise ValueError(pair)


# ---------------- persistent state ----------------

def load_state():
    if os.path.exists(cfg.FILES["state"]):
        with open(cfg.FILES["state"]) as f:
            return json.load(f)
    return {"day": "", "week": "", "day_r": 0.0, "week_r": 0.0,
            "traded_today": [], "tracked": {}}   # tracked: trade_id -> meta


def save_state(s):
    tmp = cfg.FILES["state"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, cfg.FILES["state"])


def roll_periods(s, now):
    day = now.strftime("%Y-%m-%d")
    week = f"{now.isocalendar()[0]}-{now.isocalendar()[1]}"
    if s["day"] != day:
        s["day"], s["day_r"], s["traded_today"] = day, 0.0, []
    if s["week"] != week:
        s["week"], s["week_r"] = week, 0.0


# ---------------- journal ----------------

JOURNAL_COLS = ["entry_time", "exit_time", "pair", "direction", "entry", "exit",
                "sl_initial", "tp", "units", "risk_usd", "spread_pips_at_entry",
                "r_mult", "pnl_usd", "reason", "mfe_r", "mae_r", "be_moved"]


def journal_write(row):
    new = not os.path.exists(cfg.FILES["journal"])
    with open(cfg.FILES["journal"], "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------- main cycle ----------------

def manage_open(client, s, m15_by_pair):
    """Detect closed trades (book R, journal them); update MFE/MAE; BE moves."""
    open_ids = {t["id"]: t for t in client.open_trades()}
    for tid in list(s["tracked"]):
        meta = s["tracked"][tid]
        pair, spec = meta["pair"], cfg.PAIR_SPECS[meta["pair"]]

        if tid not in open_ids:                      # trade closed
            tr = client.trade(tid)
            pnl = float(tr.get("realizedPL", 0.0))
            r = pnl / meta["risk_usd"]
            s["day_r"] += r
            s["week_r"] += r
            price = float(tr.get("averageClosePrice", meta["entry"]))
            journal_write({
                "entry_time": meta["entry_time"], "exit_time": tr.get("closeTime", ""),
                "pair": pair, "direction": meta["dir"], "entry": meta["entry"],
                "exit": price, "sl_initial": meta["sl"], "tp": meta["tp"],
                "units": meta["units"], "risk_usd": round(meta["risk_usd"], 2),
                "spread_pips_at_entry": meta["spread_pips"],
                "r_mult": round(r, 3), "pnl_usd": round(pnl, 2),
                "reason": ("target" if r >= 0.8 * meta.get("tp_r", 1.5)
                           else "be" if meta["be_moved"] and r > -0.3
                           else "stop" if r < -0.5 else "manual"),
                "mfe_r": round(meta["mfe_r"], 3), "mae_r": round(meta["mae_r"], 3),
                "be_moved": meta["be_moved"],
            })
            log.info(f"CLOSED {pair} {r:+.2f}R (day {s['day_r']:+.2f}R, week {s['week_r']:+.2f}R)")
            del s["tracked"][tid]
            continue

        # update excursions + breakeven from the last closed M15 bar
        bars = m15_by_pair.get(pair) or []
        if not bars:
            continue
        b = bars[-1]
        d, entry, risk = meta["dir"], meta["entry"], meta["risk_dist"]
        fav = d * ((b["h"] if d == 1 else b["l"]) - entry) / risk
        adv = d * ((b["l"] if d == 1 else b["h"]) - entry) / risk
        meta["mfe_r"] = max(meta["mfe_r"], fav)
        meta["mae_r"] = min(meta["mae_r"], adv)
        if not meta["be_moved"]:
            close_r = d * (b["c"] - entry) / risk
            if close_r >= P["be_trigger_r"]:
                be_px = entry + d * P["be_offset_pips"] * spec["pip"]
                try:
                    client.move_stop(tid, be_px, spec["precision"])
                    meta["be_moved"] = True
                    log.info(f"BE move {pair} -> {be_px}")
                except Exception as ex:
                    log.warning(f"BE move failed {pair}: {ex}")


def try_enter(client, news, s, pair, bars, now):
    spec = cfg.PAIR_SPECS[pair]
    if pair in s["traded_today"]:
        return
    if any(m["pair"] == pair for m in s["tracked"].values()):
        return
    if len(s["tracked"]) >= P["max_open_positions"]:
        return
    if s["day_r"] <= P["daily_stop_r"] or s["week_r"] <= P["weekly_stop_r"]:
        return
    if not (P["entry_hours"][0] <= now.hour < P["entry_hours"][1]):
        return

    h1 = client.candles_h1(pair)
    atr = atr_from_h1(h1)
    sig = evaluate_day(bars, atr, pair)
    if not sig:
        return

    blocked, why = news.blackout(pair, now)
    if blocked:
        log.info(f"signal on {pair} VETOED by news guard: {why}")
        s["traded_today"].append(pair)   # setup consumed; don't chase it later
        return

    bid, ask = client.pricing(pair)
    spread_pips = (ask - bid) / spec["pip"]
    if spread_pips > spec["max_spread_pips"]:
        log.info(f"signal on {pair} skipped: spread {spread_pips:.1f}p > cap")
        s["traded_today"].append(pair)
        return

    mid = (bid + ask) / 2
    d = sig["dir"]
    entry_est = ask if d == 1 else bid
    sl_px = round(entry_est - d * sig["risk"], spec["precision"])
    tp_px = round(entry_est + d * sig["reward"], spec["precision"])
    nav = client.nav()
    risk_usd = nav * P["risk_per_trade"]
    units = int(units_for_risk(pair, risk_usd, sig["risk"], mid)) * d
    if units == 0:
        return

    resp = client.market_order(pair, units, sl_px, tp_px, spec["precision"])
    fill = resp.get("orderFillTransaction")
    if not fill:
        log.warning(f"order NOT filled {pair}: {json.dumps(resp)[:400]}")
        s["traded_today"].append(pair)
        return
    tid = fill["tradeOpened"]["tradeID"]
    entry_px = float(fill["price"])
    s["traded_today"].append(pair)
    s["tracked"][tid] = {
        "pair": pair, "dir": d, "entry": entry_px, "sl": sl_px, "tp": tp_px,
        "units": units, "risk_usd": risk_usd, "risk_dist": sig["risk"],
        "spread_pips": round(spread_pips, 2), "entry_time": now.isoformat(),
        "tp_r": sig["reward"] / sig["risk"],
        "mfe_r": 0.0, "mae_r": 0.0, "be_moved": False,
    }
    log.info(f"ENTER {pair} {'LONG' if d==1 else 'SHORT'} {units}u @ {entry_px} "
             f"SL {sl_px} TP {tp_px} spread {spread_pips:.1f}p risk ${risk_usd:.0f}")


def cycle(client, news, s):
    now = datetime.now(timezone.utc)
    roll_periods(s, now)

    if os.path.exists(cfg.FILES["flatten_flag"]):
        for tid in list(s["tracked"]):
            try:
                client.close_trade(tid)
                log.warning(f"FLATTEN: closed trade {tid}")
            except Exception as ex:
                log.error(f"FLATTEN failed on {tid}: {ex}")
        # closes will be journaled by manage_open on this same cycle

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m15 = {}
    for pair in cfg.PAIRS:
        try:
            m15[pair] = client.candles_m15_today(pair, day_start.isoformat())
        except Exception as ex:
            log.warning(f"candle fetch failed {pair}: {ex}")

    manage_open(client, s, m15)

    halted = os.path.exists(cfg.FILES["halt_flag"]) or os.path.exists(cfg.FILES["flatten_flag"])
    if not halted:
        for pair in cfg.PAIRS:
            if pair in m15 and m15[pair]:
                try:
                    try_enter(client, news, s, pair, m15[pair], now)
                except Exception as ex:
                    log.error(f"entry attempt failed {pair}: {ex}")
    save_state(s)


def sleep_to_next_m15():
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=10, microsecond=0)
    time.sleep(max((nxt - now).total_seconds(), 5))


def main():
    log.info("ARS bot starting (practice account, forward demo test)")
    client = OandaClient()
    news = NewsGuard(log)
    s = load_state()
    log.info(f"state: day_r {s['day_r']:+.2f}R week_r {s['week_r']:+.2f}R "
             f"tracked {list(s['tracked'])}")
    while True:
        try:
            cycle(client, news, s)
        except Exception as ex:
            log.error(f"cycle error: {ex}")
        sleep_to_next_m15()


if __name__ == "__main__":
    main()
