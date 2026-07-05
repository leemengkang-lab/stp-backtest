"""
Data layer.
- fetch_oanda(): paginated M15 candle download from OANDA practice API, cached to CSV.
- load_news(): optional high-impact calendar CSV.
- synthetic(): regime-switching random-walk generator so the whole pipeline can be
  smoke-tested without market data. NEVER draw strategy conclusions from synthetic runs.
"""
import os
import numpy as np
import pandas as pd

import config


def _cache_path(pair: str, start: str, end: str) -> str:
    os.makedirs(config.OANDA["cache_dir"], exist_ok=True)
    return os.path.join(config.OANDA["cache_dir"], f"{pair}_M15_{start}_{end}.csv")


def fetch_oanda(pair: str, start: str, end: str) -> pd.DataFrame:
    """Download M15 mid candles [start, end) (ISO dates). Cached to CSV."""
    cp = _cache_path(pair, start, end)
    if os.path.exists(cp):
        df = pd.read_csv(cp, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        return df

    import requests  # imported lazily so synthetic mode needs no network deps
    token = os.environ.get(config.OANDA["env_token"])
    if not token:
        raise RuntimeError(f"Set {config.OANDA['env_token']} environment variable.")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{config.OANDA['api_url']}/v3/instruments/{pair}/candles"

    frames, cursor = [], pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    while cursor < end_ts:
        params = {
            "granularity": config.OANDA["granularity"],
            "price": config.OANDA["price"],
            "from": cursor.isoformat(),
            "count": 5000,
        }
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        candles = r.json().get("candles", [])
        if not candles:
            break
        rows = [
            {
                "time": c["time"],
                "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
                "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"]),
                "volume": c["volume"], "complete": c["complete"],
            }
            for c in candles if c["complete"]
        ]
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        last = pd.Timestamp(rows[-1]["time"])
        if last <= cursor:
            break
        cursor = last + pd.Timedelta(minutes=15)

    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    df = df[~df.index.duplicated()]
    df = df.loc[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index < end_ts)]
    df[["open", "high", "low", "close", "volume"]].to_csv(cp)
    return df[["open", "high", "low", "close", "volume"]]


def load_news() -> pd.DataFrame | None:
    """Load news_calendar.csv if present: timestamp_utc,currency,impact[,event]."""
    path = config.NEWS["csv_path"]
    if not os.path.exists(path):
        print(f"[warn] {path} not found - backtest will run WITHOUT news blackouts. "
              f"Live results will differ; source a historical high-impact calendar.")
        return None
    df = pd.read_csv(path)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df = df[df["impact"].str.lower() == "high"].copy()
    if "event" not in df.columns:
        df["event"] = ""
    return df


def synthetic(pair: str, start: str, end: str, seed: int = 0) -> pd.DataFrame:
    """Regime-switching GBM-ish M15 series for pipeline smoke tests only."""
    rng = np.random.default_rng(seed + hash(pair) % 1000)
    idx = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    # drop weekends like real FX data
    idx = idx[~idx.dayofweek.isin([5, 6])]
    n = len(idx)
    base = 150.0 if "JPY" in pair else 1.10
    pip = config.PAIR_SPECS[pair]["pip"]

    # regime: alternating trend/range blocks of ~2 weeks
    block = 96 * 10
    drift = np.zeros(n)
    for i in range(0, n, block):
        mode = rng.choice(["up", "down", "range"], p=[0.35, 0.35, 0.30])
        d = {"up": 0.06, "down": -0.06, "range": 0.0}[mode] * pip
        drift[i:i + block] = d
    # session-dependent volatility (quiet Asia, busy London/NY)
    hour = idx.hour.to_numpy()
    vol = np.where((hour >= 7) & (hour < 16), 3.2, 1.6) * pip
    ret = drift + rng.normal(0, 1, n) * vol
    close = base + np.cumsum(ret)
    o = np.concatenate([[base], close[:-1]])
    spread_hl = np.abs(rng.normal(0, 1.6, n)) * vol
    high = np.maximum(o, close) + spread_hl
    low = np.minimum(o, close) - spread_hl
    return pd.DataFrame(
        {"open": o, "high": high, "low": low, "close": close, "volume": 100},
        index=idx,
    )


def get_data(pair: str, start: str, end: str, source: str = "oanda") -> pd.DataFrame:
    if source == "synthetic":
        return synthetic(pair, start, end)
    return fetch_oanda(pair, start, end)
