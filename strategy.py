"""
Strategy layer: indicators + STP signal generation.

Lookahead protection is the critical design point here:
- All M15 signals use the *closed* bar; entries execute at the NEXT bar's open (engine).
- H1/H4 indicator values are mapped onto the M15 index using each higher-TF bar's
  END time, then forward-filled - so at any M15 timestamp we only ever see the last
  COMPLETED H1/H4 bar. This is the classic bug in retail multi-timeframe backtests
  and the reason many look profitable until they trade live.
"""
import numpy as np
import pandas as pd

import config


# ---------- indicators ----------

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _resample(m15: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return m15.resample(rule, label="left", closed="left").agg(agg).dropna()


def _map_to_m15(htf: pd.DataFrame, cols: list[str], m15_index: pd.DatetimeIndex,
                bar_len: pd.Timedelta) -> pd.DataFrame:
    """Reindex higher-TF columns onto M15 using bar END times (no lookahead)."""
    shifted = htf[cols].copy()
    shifted.index = shifted.index + bar_len  # value becomes known at bar close
    return shifted.reindex(m15_index, method="ffill")


# ---------- feature build ----------

def build_features(m15: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = params
    h1, h4 = _resample(m15, "1h"), _resample(m15, "4h")

    h1["ema_f"] = ema(h1["close"], p["ema_fast"])
    h1["atr"] = atr(h1, p["atr_period"])
    h1["atr_sma"] = h1["atr"].rolling(50).mean()
    h1["swing_low"] = h1["low"].rolling(10).min()
    h1["swing_high"] = h1["high"].rolling(10).max()

    h4["ema_f"] = ema(h4["close"], p["ema_fast"])
    h4["ema_s"] = ema(h4["close"], p["ema_slow"])

    f = m15.copy()
    f["rsi"] = rsi(f["close"], p["rsi_period"])
    f = f.join(_map_to_m15(h1, ["close", "ema_f", "atr", "atr_sma", "swing_low", "swing_high"],
                           f.index, pd.Timedelta(hours=1)).add_prefix("h1_"))
    f = f.join(_map_to_m15(h4, ["close", "ema_f", "ema_s"],
                           f.index, pd.Timedelta(hours=4)).add_prefix("h4_"))
    return f.dropna()


# ---------- filters ----------

def in_session(ts: pd.Timestamp, pair: str) -> bool:
    h = ts.hour
    for s in config.SESSIONS.values():
        lo, hi = s["hours"]
        if lo <= h < hi and pair in s["pairs"]:
            return True
    return False


def build_news_blackout(news: pd.DataFrame | None, pair: str,
                        index: pd.DatetimeIndex) -> pd.Series:
    """Boolean Series on the M15 index: True = blackout (no entries)."""
    out = pd.Series(False, index=index)
    if news is None:
        return out
    ccys = config.NEWS["currency_map"][pair]
    rel = news[news["currency"].isin(ccys)]
    before = pd.Timedelta(minutes=config.NEWS["blackout_before_min"])
    after = pd.Timedelta(minutes=config.NEWS["blackout_after_min"])
    extra = pd.Timedelta(minutes=config.NEWS["tier1_events_extra_min"])
    kw = [k.lower() for k in config.NEWS["tier1_keywords"]]
    for _, row in rel.iterrows():
        pad = extra if any(k in str(row["event"]).lower() for k in kw) else pd.Timedelta(0)
        mask = (index >= row["timestamp_utc"] - before - pad) & \
               (index <= row["timestamp_utc"] + after + pad)
        out |= pd.Series(mask, index=index)
    return out


# ---------- signals ----------

def generate_signals(f: pd.DataFrame, pair: str, params: dict,
                     blackout: pd.Series) -> pd.DataFrame:
    """Return f with columns: signal (+1/-1/0), sl_dist, tp_dist (price units)."""
    p = params
    rsi_l, rsi_s = p["rsi_long_level"], 100 - p["rsi_long_level"]

    regime_long = (f["h4_close"] > f["h4_ema_f"]) & (f["h4_ema_f"] > f["h4_ema_s"])
    regime_short = (f["h4_close"] < f["h4_ema_f"]) & (f["h4_ema_f"] < f["h4_ema_s"])
    vol_ok = f["h1_atr"] >= p["atr_min_ratio"] * f["h1_atr_sma"]

    cross_up = (f["rsi"] > rsi_l) & (f["rsi"].shift(1) <= rsi_l)
    cross_dn = (f["rsi"] < rsi_s) & (f["rsi"].shift(1) >= rsi_s)

    near = p["pullback_atr_dist"] * f["h1_atr"]
    near_long = ((f["close"] - f["h1_ema_f"]).abs() <= near) | \
                ((f["close"] - f["h1_swing_low"]).abs() <= near)
    near_short = ((f["close"] - f["h1_ema_f"]).abs() <= near) | \
                 ((f["close"] - f["h1_swing_high"]).abs() <= near)

    session_ok = pd.Series([in_session(ts, pair) for ts in f.index], index=f.index)

    long_sig = regime_long & (f["close"] > f["h1_ema_f"]) & cross_up & near_long
    short_sig = regime_short & (f["close"] < f["h1_ema_f"]) & cross_dn & near_short
    ok = vol_ok & session_ok & ~blackout.reindex(f.index, fill_value=False)

    f = f.copy()
    f["signal"] = np.where(long_sig & ok, 1, np.where(short_sig & ok, -1, 0))

    spec = config.PAIR_SPECS[pair]
    sl = (p["sl_atr_mult"] * f["h1_atr"]).clip(lower=spec["min_sl_pips"] * spec["pip"])
    f["sl_dist"] = sl
    f["tp_dist"] = p["tp_atr_mult"] * f["h1_atr"]
    return f
