"""poe2scout API client.

Endpoints (base https://api.poe2scout.com, realm path "poe2", no API key):
  GET /poe2/Leagues                                      -> league list, is_current flag
  GET /poe2/Leagues/{value}/Items                        -> all uniques + currencies
  GET /poe2/Leagues/{value}/Items/{id}/History           -> {price_history, has_more}

Constraints handled here:
  - LogCount must be divisible by 4
  - league value must be URL-encoded (contains spaces)
  - response keys may be snake_case / camelCase / PascalCase -> field() helper
  - rate limit ~100/min -> throttle + exponential backoff on 429
  - prices come back in Exalted when no ReferenceCurrency is passed
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

BASE = "https://api.poe2scout.com"
REALM = "poe2"
LOG_COUNT = 1000          # points per History call, must be a multiple of 4
MIN_INTERVAL = 0.62       # seconds between requests (~97/min, under the ~100/min limit)
MAX_RETRIES = 6


def _norm(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def field(obj: dict, *names, default=None):
    """Read a key regardless of casing style (item_id / itemId / ItemId)."""
    table = {_norm(k): v for k, v in obj.items()}
    for n in names:
        if _norm(n) in table:
            return table[_norm(n)]
    return default


def parse_ts(value: str) -> str:
    """Normalize an API timestamp to ISO-8601 UTC ('YYYY-MM-DDTHH:MM:SS+00:00').

    Python 3.10 fromisoformat() cannot parse a trailing 'Z', so replace it first.
    Stored normalized so string comparison == chronological comparison.
    """
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Client:
    email: str = "anon@example.com"
    _last_request: float = dc_field(default=0.0, repr=False)

    @property
    def user_agent(self) -> str:
        return f"poe2-investment-analyzer (contact: {self.email})"

    def _throttle(self) -> None:
        wait = self._last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path: str, params: dict | None = None):
        url = f"{BASE}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(min(60, 2 ** (attempt + 1)))
                    continue
                if e.code in (400, 404):
                    return None
                if e.code >= 500:
                    time.sleep(min(30, 2**attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(30, 2**attempt))
        raise RuntimeError(f"API request kept failing after {MAX_RETRIES} tries: {url}")

    # ---- typed endpoints -------------------------------------------------

    def leagues(self) -> list[dict]:
        return self.get(f"{REALM}/Leagues") or []

    def current_league(self) -> dict:
        leagues = self.leagues()
        if not leagues:
            raise RuntimeError("Could not load league list from api.poe2scout.com")
        for lg in leagues:
            if field(lg, "is_current", "isCurrent", "current_league", default=False):
                return lg
        return leagues[0]

    def items(self, league_value: str) -> list[dict]:
        league = urllib.parse.quote(league_value)
        return self.get(f"{REALM}/Leagues/{league}/Items") or []

    def history_page(
        self,
        league_value: str,
        item_id: int,
        end_time: str | None = None,
        log_count: int = LOG_COUNT,
        reference_currency: str | None = None,
    ) -> tuple[list[dict], bool]:
        """One History page: ([{ts, price, quantity}, ...] sorted ascending, has_more).

        Prices are in Exalted when reference_currency is None (the API base unit).
        """
        assert log_count % 4 == 0, "LogCount must be divisible by 4"
        league = urllib.parse.quote(league_value)
        data = self.get(
            f"{REALM}/Leagues/{league}/Items/{item_id}/History",
            {
                "LogCount": log_count,
                "ReferenceCurrency": reference_currency,
                "EndTime": end_time,
            },
        )
        if not data:
            return [], False
        raw = field(data, "price_history", "priceHistory", default=[]) or []
        points = []
        for p in raw:
            t, price = field(p, "time"), field(p, "price")
            if t is None or price is None:
                continue
            qty = field(p, "quantity", default=0)
            points.append(
                {
                    "ts": parse_ts(str(t)),
                    "price": float(price),
                    "quantity": int(qty or 0),
                }
            )
        points.sort(key=lambda p: p["ts"])
        has_more = bool(field(data, "has_more", "hasMore", default=False))
        return points, has_more
