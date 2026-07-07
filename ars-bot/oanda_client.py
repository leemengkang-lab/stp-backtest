"""Thin OANDA v20 REST wrapper. Practice endpoint only. No pandas — e2-micro friendly."""
import os
import time

import requests

import bot_config as cfg


class OandaClient:
    def __init__(self):
        token = os.environ.get(cfg.OANDA["env_token"])
        self.account = os.environ.get(cfg.OANDA["env_account"])
        if not token or not self.account:
            raise RuntimeError(f"Set {cfg.OANDA['env_token']} and {cfg.OANDA['env_account']}")
        self.base = cfg.OANDA["api_url"]
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._home_ccy = None       # account (home) currency, e.g. "USD" or "SGD"
        self._home_rate = None      # home-currency units per 1 USD (cached)

    def _get(self, path, **params):
        for attempt in range(3):
            try:
                r = requests.get(self.base + path, headers=self.h, params=params, timeout=20)
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    # ---- market data ----
    def candles_m15_today(self, pair, day_start_iso):
        j = self._get(f"/v3/instruments/{pair}/candles",
                      granularity="M15", price="M", **{"from": day_start_iso}, count=110)
        return [
            {"time": c["time"], "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]),
             "l": float(c["mid"]["l"]), "c": float(c["mid"]["c"])}
            for c in j["candles"] if c["complete"]
        ]

    def candles_h1(self, pair, count=60):
        j = self._get(f"/v3/instruments/{pair}/candles",
                      granularity="H1", price="M", count=count)
        return [
            {"h": float(c["mid"]["h"]), "l": float(c["mid"]["l"]), "c": float(c["mid"]["c"])}
            for c in j["candles"] if c["complete"]
        ]

    def pricing(self, pair):
        j = self._get(f"/v3/accounts/{self.account}/pricing", instruments=pair)
        p = j["prices"][0]
        return float(p["bids"][0]["price"]), float(p["asks"][0]["price"])

    # ---- account / trades ----
    def nav(self):
        return float(self._get(f"/v3/accounts/{self.account}/summary")["account"]["NAV"])

    def home_per_usd(self):
        """Account (home) currency units per 1 USD; 1.0 for a USD account. Cached.

        Needed to size positions in the ACCOUNT currency: OANDA reports NAV and
        realized P&L in the home currency, but per-unit P&L is in the pair's
        quote currency, so a non-USD account must convert through this rate.
        """
        if self._home_rate is None:
            summ = self._get(f"/v3/accounts/{self.account}/summary")["account"]
            self._home_ccy = summ["currency"]
            if self._home_ccy == "USD":
                self._home_rate = 1.0
            else:
                try:                                    # e.g. USD_SGD -> SGD per USD
                    bid, ask = self.pricing(f"USD_{self._home_ccy}")
                    self._home_rate = (bid + ask) / 2
                except Exception:                       # fall back to XXX_USD inverted
                    bid, ask = self.pricing(f"{self._home_ccy}_USD")
                    self._home_rate = 1.0 / ((bid + ask) / 2)
        return self._home_rate

    def open_trades(self):
        return self._get(f"/v3/accounts/{self.account}/openTrades")["trades"]

    def trade(self, trade_id):
        return self._get(f"/v3/accounts/{self.account}/trades/{trade_id}")["trade"]

    def market_order(self, pair, units, sl_price, tp_price, precision):
        body = {"order": {
            "type": "MARKET", "instrument": pair, "units": str(int(units)),
            "timeInForce": "FOK", "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl_price:.{precision}f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.{precision}f}"},
        }}
        r = requests.post(f"{self.base}/v3/accounts/{self.account}/orders",
                          headers=self.h, json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def move_stop(self, trade_id, price, precision):
        body = {"stopLoss": {"price": f"{price:.{precision}f}", "timeInForce": "GTC"}}
        r = requests.put(f"{self.base}/v3/accounts/{self.account}/trades/{trade_id}/orders",
                         headers=self.h, json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def close_trade(self, trade_id):
        r = requests.put(f"{self.base}/v3/accounts/{self.account}/trades/{trade_id}/close",
                         headers=self.h, json={}, timeout=20)
        r.raise_for_status()
        return r.json()
