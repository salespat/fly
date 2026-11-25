"""OpenSky API client with a daily credit budget and polite 429 handling.

OpenSky gives each account a daily allowance of credits for the /flights/*
endpoints (anonymous 400, standard account 4,000, active feeders 8,000). Querying
past days costs ~30 credits per request (1-2 UTC days per request), so a full
backfill uses most of a standard allowance. This client:

* estimates the cost of every request and refuses to exceed ``OPENSKY_DAILY_CREDITS``
  (default 3,500, leaving headroom below the 4,000 allowance),
* stops all OpenSky calls for the rest of the run on HTTP 429 and remembers
  ``X-Rate-Limit-Retry-After-Seconds`` so later runs don't hammer the API,
* records spend in the pipeline cache so the budget survives between builds.
"""
from __future__ import annotations

import datetime as dt
import os
import time

from .common import CACHE, UA, log, read_json, write_json

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
LEDGER = CACHE / "opensky_budget.json"


class OpenSkyLimit(Exception):
    """Credits exhausted (HTTP 429) or our own daily budget reached."""


def credit_cost(begin: int, end: int, now: float | None = None) -> int:
    """Conservative cost estimate from OpenSky's /flights credit table (by UTC day partitions)."""
    now = now or time.time()
    days = (dt.datetime.fromtimestamp(end - 1, dt.timezone.utc).date()
            - dt.datetime.fromtimestamp(begin, dt.timezone.utc).date()).days + 1
    if end > now - 86400 and days <= 1:
        return 4
    if days <= 2:
        return 30
    for top, per in ((10, 60), (15, 120), (20, 240), (25, 480)):
        if days <= top:
            return per * days
    return 960 * days


class OpenSkyClient:
    def __init__(self, client_id: str | None, client_secret: str | None, daily_credits: int | None = None,
                 ledger_path=LEDGER):
        self.client_id, self.client_secret = client_id, client_secret
        default = 3500 if client_id and client_secret else 350
        self.daily = int(daily_credits or os.environ.get("OPENSKY_DAILY_CREDITS") or default)
        self.ledger_path = ledger_path
        self._token, self._exp = None, 0.0
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        led = read_json(ledger_path, {}) or {}
        self.ledger = {"date": today, "spent": led.get("spent", 0) if led.get("date") == today else 0,
                       "blocked_until": led.get("blocked_until", 0), "remaining": led.get("remaining")}
        self.requests = 0
        self.stopped_reason = ""

    # ---------------------------------------------------------------- budget
    def _save(self):
        write_json(self.ledger_path, self.ledger, compact=False)

    def blocked(self) -> str:
        if self.stopped_reason:
            return self.stopped_reason
        until = self.ledger.get("blocked_until") or 0
        if time.time() < until:
            mins = round((until - time.time()) / 60)
            self.stopped_reason = f"OpenSky credits exhausted (HTTP 429); retrying in ~{mins} min"
        return self.stopped_reason

    def summary(self) -> dict:
        return {"creditsSpentToday": self.ledger["spent"], "dailyBudget": self.daily, "requests": self.requests,
                "remainingReported": self.ledger.get("remaining"), "stopped": self.stopped_reason or None}

    # ---------------------------------------------------------------- http
    def _headers(self) -> dict:
        h = dict(UA)
        if not (self.client_id and self.client_secret):
            return h
        if time.time() > self._exp - 60:
            import requests
            r = requests.post(TOKEN_URL, timeout=30, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id, "client_secret": self.client_secret})
            r.raise_for_status()
            j = r.json()
            self._token, self._exp = j["access_token"], time.time() + j.get("expires_in", 1800)
        h["Authorization"] = f"Bearer {self._token}"
        return h

    def get(self, url: str, params: dict) -> list[dict]:
        """GET a /flights endpoint. Returns [] on 404. Raises OpenSkyLimit on 429/budget."""
        import requests
        if self.blocked():
            raise OpenSkyLimit(self.stopped_reason)
        cost = credit_cost(int(params["begin"]), int(params["end"]))
        if self.ledger["spent"] + cost > self.daily:
            self.stopped_reason = f"daily OpenSky budget reached ({self.ledger['spent']}/{self.daily} credits); continuing next UTC day"
            raise OpenSkyLimit(self.stopped_reason)
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, headers=self._headers(), timeout=120)
            except requests.RequestException as e:
                if attempt == 2:
                    raise
                log.warning("OpenSky network error (%s); retrying", e)
                time.sleep(5 * (attempt + 1))
                continue
            self.requests += 1
            rem = r.headers.get("X-Rate-Limit-Remaining")
            if rem and rem.lstrip("-").isdigit():
                self.ledger["remaining"] = int(rem)
            if r.status_code == 429:
                wait = r.headers.get("X-Rate-Limit-Retry-After-Seconds") or r.headers.get("Retry-After") or "3600"
                wait = int(float(wait)) if str(wait).replace(".", "", 1).isdigit() else 3600
                self.ledger["blocked_until"] = int(time.time()) + min(max(wait, 300), 26 * 3600)
                self.ledger["spent"] = max(self.ledger["spent"], self.daily)  # treat today as used up
                self._save()
                self.stopped_reason = f"OpenSky credits exhausted (HTTP 429); retrying in ~{round(wait / 60)} min"
                log.warning(self.stopped_reason)
                raise OpenSkyLimit(self.stopped_reason)
            if r.status_code >= 500:
                log.warning("OpenSky HTTP %s; retrying", r.status_code)
                time.sleep(10 * (attempt + 1))
                continue
            self.ledger["spent"] += cost
            self._save()
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json() or []
        raise RuntimeError(f"OpenSky unavailable for {url}")
