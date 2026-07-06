"""News guard: fetches the Forex Factory weekly calendar and answers one
question — is `pair` inside a high-impact blackout window right now?

Fail-safe: if the calendar can't be fetched (and config says so), the bot
takes no new entries rather than trading blind through news."""
import time
from datetime import datetime, timezone, timedelta

import requests

import bot_config as cfg


class NewsGuard:
    def __init__(self, log):
        self.log = log
        self.events = None          # list of (utc_datetime, currency, is_tier1)
        self.fetched_at = 0.0

    def refresh(self):
        if self.events is not None and time.time() - self.fetched_at < 6 * 3600:
            return
        try:
            r = requests.get(cfg.NEWS["url"], timeout=20,
                             headers={"User-Agent": "ars-bot/1.0"})
            r.raise_for_status()
            raw = r.json()
            evts = []
            for e in raw:
                if str(e.get("impact", "")).lower() != "high":
                    continue
                # FF dates are ISO with offset, e.g. "2026-07-06T08:30:00-04:00"
                dt = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
                title = str(e.get("title", "")).lower()
                tier1 = any(k in title for k in cfg.NEWS["tier1_keywords"])
                evts.append((dt, str(e.get("country", "")).upper(), tier1))
            self.events = evts
            self.fetched_at = time.time()
            self.log.info(f"news calendar refreshed: {len(evts)} high-impact events this week")
        except Exception as ex:
            self.log.warning(f"news calendar fetch FAILED: {ex}")
            if self.events is None:
                self.events = None   # stays unavailable

    def blackout(self, pair, now_utc: datetime):
        """Returns (blocked: bool, reason: str)."""
        self.refresh()
        if self.events is None:
            if cfg.NEWS["halt_entries_without_calendar"]:
                return True, "calendar unavailable (fail-safe halt)"
            return False, ""
        ccys = cfg.NEWS["currency_map"][pair]
        before = timedelta(minutes=cfg.NEWS["blackout_before_min"])
        after = timedelta(minutes=cfg.NEWS["blackout_after_min"])
        extra = timedelta(minutes=cfg.NEWS["tier1_extra_min"])
        for dt, ccy, tier1 in self.events:
            if ccy not in ccys:
                continue
            pad = extra if tier1 else timedelta(0)
            if dt - before - pad <= now_utc <= dt + after + pad:
                return True, f"{ccy} high-impact event at {dt.strftime('%H:%M')} UTC"
        return False, ""
