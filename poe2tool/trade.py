"""Watchlist: poll saved searches on the official PoE2 trade API.

Why this exists: poe2scout/poe.ninja only aggregate *identified* items, so e.g.
unidentified 'Voices' Sapphires (whose value depends on the unrolled socket
count) have no public price history anywhere. The official trade API has no
history either - but it serves current listings without login, so we record
our own history going forward, one snapshot per poll.

Endpoints (verified live; the HTML pages are Cloudflare-challenged, the API
paths are not):
  GET  /api/trade2/search/poe2/{league}/{searchId}  -> saved query as JSON
  POST /api/trade2/search/poe2/{league}             -> {id, result: [hashes], total}
  GET  /api/trade2/fetch/{h1,...,h10}?query={id}    -> listings incl. price

Rate limits come back in X-Rate-Limit headers (search: 5/10s, 15/60s, 30/300s).
One poll needs ~3 requests per watched search - hourly polling is far below
any limit, but we still throttle and honor Retry-After on 429.

Prices are converted to Exalted (the tool's base currency) using the newest
poe2scout rates in the local DB; each poll first refreshes the reference
currencies (divine/chaos) so the conversion never goes stale.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from statistics import median

from . import db
from .api import Client as ScoutClient

TRADE_BASE = "https://www.pathofexile.com/api/trade2"
# browser-like UA: this is what was verified to pass; script-y UAs risk blocks
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MIN_INTERVAL = 1.6
MAX_RETRIES = 5
TOP_N = 20          # cheapest listings per snapshot (2 fetch calls of 10)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeClient:
    def __init__(self):
        self._last = 0.0

    def _request(self, url: str, body: dict | None = None):
        wait = self._last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = e.headers.get("Retry-After")
                    time.sleep(int(retry) if retry and retry.isdigit()
                               else min(120, 5 * 2**attempt))
                    continue
                if e.code >= 500:
                    time.sleep(min(60, 2**attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(60, 2**attempt))
        raise RuntimeError(f"trade API kept failing: {url}")

    def saved_query(self, league: str, search_id: str) -> dict:
        lg = urllib.parse.quote(league)
        data = self._request(f"{TRADE_BASE}/search/poe2/{lg}/{search_id}")
        if not data or "query" not in data:
            raise RuntimeError(f"search {search_id} returned no query "
                               "(deleted or wrong league?)")
        return data["query"]

    def search(self, league: str, query: dict) -> dict:
        lg = urllib.parse.quote(league)
        return self._request(f"{TRADE_BASE}/search/poe2/{lg}",
                             {"query": query, "sort": {"price": "asc"}})

    def fetch(self, hashes: list[str], query_id: str) -> list[dict]:
        out = []
        for i in range(0, len(hashes), 10):
            chunk = ",".join(hashes[i:i + 10])
            data = self._request(f"{TRADE_BASE}/fetch/{chunk}?query={query_id}")
            out.extend(r for r in (data or {}).get("result", []) if r)
        return out


# --------------------------------------------------------------------------

def parse_search_ref(ref: str, default_league: str | None) -> tuple[str, str]:
    """Accepts a full trade URL or a bare search id -> (league, search_id).
    Raises ValueError (not SystemExit - callers include the dashboard, where
    an uncaught SystemExit would take the whole app process down)."""
    if "/" in ref:
        parts = [p for p in urllib.parse.urlparse(ref).path.split("/") if p]
        # .../trade2/search/poe2/{league}/{id}
        if len(parts) >= 2:
            return urllib.parse.unquote(parts[-2]), parts[-1]
        raise ValueError(f"Could not parse trade URL: {ref}")
    if not default_league:
        raise ValueError("Bare search id given but no league in the DB yet - "
                         "run `collect` once or pass the full trade URL.")
    return default_league, ref


def auto_label(query: dict) -> str:
    name = query.get("name") or ""
    typ = query.get("type") or ""
    if isinstance(name, dict):
        name = name.get("option", "")
    if isinstance(typ, dict):
        typ = typ.get("option", "")
    ident = (query.get("filters", {}).get("misc_filters", {})
             .get("filters", {}).get("identified", {}).get("option"))
    suffix = {"true": " [id]", "false": " [unid]"}.get(str(ident).lower(), "")
    label = f"{name} {typ}".strip() or "trade search"
    return label + suffix


def add_search(db_path: str, ref: str, label: str | None = None) -> str:
    """Register a trade URL/search id pasted by the user (dashboard or CLI).
    Returns a status message instead of raising for anything short of a
    programming error, so the dashboard can show it with st.success/st.error
    without a page crash."""
    con = db.connect(db_path)
    try:
        league, search_id = parse_search_ref(ref, db.get_meta(con, "league"))
        query = TradeClient().saved_query(league, search_id)
    except (ValueError, RuntimeError, urllib.error.HTTPError) as e:
        con.close()
        return f"Konnte Suche nicht laden: {e}"
    final_label = label or auto_label(query)
    con.execute(
        "INSERT INTO trade_searches(search_id, label, league, query_json, active, added_ts) "
        "VALUES(?,?,?,?,1,?) ON CONFLICT(search_id) DO UPDATE SET "
        "label=excluded.label, league=excluded.league, "
        "query_json=excluded.query_json, active=1",
        (search_id, final_label, league, json.dumps(query), _now_iso()),
    )
    con.commit()
    con.close()
    return f"Watching: {final_label} ({search_id}, league {league})"


def list_searches(db_path: str) -> None:
    con = db.connect(db_path)
    rows = con.execute(
        "SELECT s.*, COUNT(t.ts) AS snapshots, MAX(t.ts) AS last_ts "
        "FROM trade_searches s LEFT JOIN trade_snapshots t USING(search_id) "
        "GROUP BY s.search_id ORDER BY s.added_ts").fetchall()
    if not rows:
        print("No watched searches. Add one: python -m poe2tool trade add <trade-URL>")
    for r in rows:
        state = "active" if r["active"] else "paused"
        print(f"{r['search_id']:12} {r['label']:32} {state:7} "
              f"{r['snapshots']:4d} snapshots, last: {r['last_ts'] or '-'}")
    con.close()


def remove_search(db_path: str, ref: str) -> None:
    con = db.connect(db_path)
    sid = ref.rstrip("/").rsplit("/", 1)[-1]
    n = con.execute("UPDATE trade_searches SET active=0 WHERE search_id=? OR label=?",
                    (sid, ref)).rowcount
    con.commit()
    con.close()
    print(f"{n} search(es) paused (snapshots are kept)." if n else f"No match for {ref!r}.")


def delete_searches(db_path: str, search_ids: list[str]) -> int:
    """Permanently remove searches + their recorded snapshots (unlike
    remove_search/pausing, this actually drops the history). Safe even if a
    favorite still points at the deleted label: set_tracking() re-creates the
    search from scratch the next time tracking is switched back on."""
    if not search_ids:
        return 0
    con = db.connect(db_path)
    placeholders = ",".join("?" * len(search_ids))
    con.execute(f"DELETE FROM trade_snapshots WHERE search_id IN ({placeholders})",
               search_ids)
    n = con.execute(f"DELETE FROM trade_searches WHERE search_id IN ({placeholders})",
                    search_ids).rowcount
    con.commit()
    con.close()
    return n


# flag name -> (trade2 misc_filters id, label tag for yes, label tag for no)
ITEM_FLAGS = {
    "identified": ("identified", "id", "unid"),
    "corrupted": ("corrupted", "corr", "uncorr"),
    "unrevealed": ("veiled", "unrev", "revealed"),  # trade2 calls it 'veiled'
}


def _flag_filters(flags: dict | None) -> dict:
    """'yes'/'no' flags -> trade2 misc_filters entries; 'any' is omitted."""
    out = {}
    for name, (api_id, _, _) in ITEM_FLAGS.items():
        v = str((flags or {}).get(name, "any")).lower()
        if v in ("yes", "no"):
            out[api_id] = {"option": "true" if v == "yes" else "false"}
    return out


def flag_label(item_name: str, flags: dict | None) -> str:
    """Deterministic watch label, so favorites can find their trade search."""
    tags = []
    for name, (_, yes_tag, no_tag) in ITEM_FLAGS.items():
        v = str((flags or {}).get(name, "any")).lower()
        if v == "yes":
            tags.append(yes_tag)
        elif v == "no":
            tags.append(no_tag)
    return (f"{item_name} [trade: {'+'.join(tags)}]" if tags
            else f"{item_name} [trade]")


def trade_url(league: str, search_id: str) -> str:
    """Browser-facing (not API) URL for a saved search - what a human clicks.
    Mirrors the shape of the URLs pathofexile.com itself hands out."""
    return f"https://www.pathofexile.com/trade2/search/poe2/{urllib.parse.quote(league)}/{search_id}"


def existing_trade_url(db_path: str, item_id: int) -> str | None:
    """Fast, API-free lookup: reuse any trade_searches row already created
    for this item's name - via favoriting, or a previous 'open trade' click -
    instead of minting a fresh search (and burning an API call) every time
    the detail page loads. Matches by label prefix, so it finds any flag
    variant; picks the most recently added one."""
    con = db.connect(db_path)
    item = con.execute("SELECT name FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not item:
        con.close()
        return None
    row = con.execute(
        "SELECT search_id, league FROM trade_searches "
        "WHERE label = ? OR label LIKE ? ORDER BY added_ts DESC LIMIT 1",
        (f"{item['name']} [trade]", f"{item['name']} [trade:%"),
    ).fetchone()
    con.close()
    return trade_url(row["league"], row["search_id"]) if row else None


def create_default_trade_url(db_path: str, item_id: int) -> tuple[str | None, str]:
    """One-off 'any flags' search for items with no existing watch yet, so
    the dashboard's 'open in trade' button has a URL to point at. Reuses
    add_item_search's dedup/creation logic; returns (url or None, status msg)."""
    flags = {"identified": "any", "corrupted": "any", "unrevealed": "any"}
    con = db.connect(db_path)
    row = con.execute("SELECT name FROM items WHERE item_id=?", (item_id,)).fetchone()
    con.close()
    if not row:
        return None, f"item {item_id} not found"
    label = flag_label(row["name"], flags)
    msg = add_item_search(db_path, item_id, flags)
    con = db.connect(db_path)
    ts = con.execute("SELECT search_id, league FROM trade_searches WHERE label=?",
                     (label,)).fetchone()
    con.close()
    return (trade_url(ts["league"], ts["search_id"]), msg) if ts else (None, msg)


def add_item_search(db_path: str, item_id: int, flags: dict | None = None) -> str:
    """Create a trade watch for a poe2scout item without needing a trade URL.

    Uniques carry their base type in items.type -> query {name, type};
    currencies/fragments (type NULL) are matched by type line -> {type: name}.
    `flags` ('any'/'yes'/'no' per ITEM_FLAGS key) become misc_filters.
    The search id returned by the first POST identifies the watch; polling
    re-runs the cached query, so the id never has to stay valid on GGG's side.
    """
    con = db.connect(db_path)
    row = con.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        con.close()
        return f"item {item_id} not found"
    league = db.get_meta(con, "league")
    label = flag_label(row["name"], flags)
    dup = con.execute("SELECT search_id FROM trade_searches WHERE label=? AND active=1",
                      (label,)).fetchone()
    if dup:
        con.close()
        return f"already watching {label} ({dup['search_id']})"

    misc = _flag_filters(flags)
    client = TradeClient()
    # "securable" = instantly buyable (incl. offline sellers) - high-value items
    # are mostly listed exactly that way; "online" would find ~0 of them
    if row["type"]:
        query = {"name": row["name"], "type": row["type"],
                 "status": {"option": "securable"}}
    else:
        query = {"type": row["name"], "status": {"option": "securable"}}
    if misc:
        query["filters"] = {"misc_filters": {"filters": misc}}
    try:
        result = client.search(league, query)
    except urllib.error.HTTPError as e:
        con.close()
        return f"trade API error for {label!r} ({e.code}) - not added"
    # fallback: unique base types occasionally shift -> retry by name only.
    # Only meaningful for uniques (row["type"] set) - the primary query for
    # currencies already IS name-based ({"type": name}), and the trade API
    # rejects a currency-category search with just "name" (400 Bad Request).
    if not result.get("total") and row["type"]:
        fallback = {"name": row["name"], "status": {"option": "securable"}}
        if misc:
            fallback["filters"] = {"misc_filters": {"filters": misc}}
        try:
            alt = client.search(league, fallback)
            if alt.get("total"):
                query, result = fallback, alt
        except urllib.error.HTTPError:
            pass  # keep the original (empty) result, fall through below
    if not result.get("total"):
        con.close()
        return (f"no live listings found for {label!r} - not added "
                "(zu strenge Flags? aktuell 0 passende Listings)")

    con.execute(
        "INSERT INTO trade_searches(search_id, label, league, query_json, active, added_ts) "
        "VALUES(?,?,?,?,1,?) ON CONFLICT(search_id) DO UPDATE SET active=1",
        (result["id"], label, league, json.dumps(query), _now_iso()),
    )
    con.commit()
    con.close()
    return (f"Watching: {label} ({result['id']}, {result['total']} listings). "
            "Snapshots ab dem nächsten `trade collect`.")


# --------------------------------------------------------------------------

def _refresh_reference_rates(con, email: str) -> None:
    """Pull the newest divine/chaos history points from poe2scout so
    exalted-conversion of fresh trade snapshots never uses stale rates."""
    league = db.get_meta(con, "league")
    if not league:
        return
    scout = ScoutClient(email=email)
    rows = con.execute(
        "SELECT item_id, api_id FROM items WHERE api_id IN ('divine','chaos')"
    ).fetchall()
    for r in rows:
        try:
            points, _ = scout.history_page(league, r["item_id"], log_count=8)
            db.insert_points(con, r["item_id"], points)
        except Exception as e:  # snapshot must not die on a scout hiccup
            print(f"  warning: rate refresh for {r['api_id']} failed: {e}")
    con.commit()


# only real price-setting currencies count for the stats; listings priced in
# anything else (e.g. "99x waystone-3") are almost always placeholder/troll
# prices and would poison min/median if converted at face value
PRICE_CURRENCIES = {"exalted", "exalt", "divine", "chaos", "mirror"}


def _exalted_rates(con) -> dict[str, float]:
    """Latest known exalted value per whitelisted currency (exalted = 1)."""
    rates = {"exalted": 1.0, "exalt": 1.0}
    rows = con.execute(
        "SELECT i.api_id, p.price_exalted FROM items i "
        "JOIN price_points p ON p.item_id = i.item_id "
        "WHERE i.api_id IS NOT NULL AND p.ts = "
        " (SELECT MAX(ts) FROM price_points WHERE item_id = i.item_id)"
    ).fetchall()
    for r in rows:
        api_id = (r["api_id"] or "").lower()
        if api_id in PRICE_CURRENCIES and r["price_exalted"]:
            rates[api_id] = float(r["price_exalted"])
    return rates


def collect_snapshots(db_path: str, email: str) -> list[str]:
    """One snapshot per active search; returns the per-search result lines
    (also printed) so the dashboard can display them."""
    con = db.connect(db_path)
    searches = con.execute(
        "SELECT * FROM trade_searches WHERE active=1").fetchall()
    if not searches:
        msg = "No active watched searches - add one with `trade add <url>`."
        print(msg)
        con.close()
        return [msg]

    _refresh_reference_rates(con, email)
    rates = _exalted_rates(con)
    client = TradeClient()
    ts = _now_iso()
    results: list[str] = []

    for s in searches:
        try:
            result = client.search(s["league"], json.loads(s["query_json"]))
            hashes = (result.get("result") or [])[:TOP_N]
            listings = client.fetch(hashes, result["id"]) if hashes else []
        except Exception as e:
            line = f"[{s['label']}] FAILED: {e}"
            print(line)
            results.append(line)
            continue

        prices_ex, skipped = [], 0
        for li in listings:
            p = (li.get("listing") or {}).get("price") or {}
            amount, cur = p.get("amount"), str(p.get("currency", "")).lower()
            if amount is None or cur not in rates:
                skipped += 1
                continue
            prices_ex.append(float(amount) * rates[cur])
        prices_ex.sort()

        if prices_ex:
            con.execute(
                "INSERT OR IGNORE INTO trade_snapshots"
                "(search_id, ts, total, n_used, min_exalted, med10_exalted, prices_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (s["search_id"], ts, result.get("total"), len(prices_ex),
                 prices_ex[0], median(prices_ex[:10]),
                 json.dumps([round(x, 4) for x in prices_ex])),
            )
            con.commit()
        div = rates.get("divine")
        shown = (f"min {prices_ex[0] / div:.4g} div, "
                 f"med10 {median(prices_ex[:10]) / div:.4g} div"
                 if prices_ex and div else "no usable prices")
        line = (f"[{s['label']}] {result.get('total', '?')} listings, "
                f"{len(prices_ex)} used ({skipped} skipped) -> {shown}")
        print(line)
        results.append(line)
    db.checkpoint(con)
    con.close()
    return results


def collect_loop(db_path: str, email: str, interval: int) -> None:
    while True:
        started = time.monotonic()
        try:
            collect_snapshots(db_path, email)
        except Exception as e:
            print(f"snapshot round failed: {e}")
        sleep_for = max(30.0, interval - (time.monotonic() - started))
        print(f"next snapshot in {sleep_for / 60:.0f} min "
              f"({datetime.now().strftime('%H:%M:%S')})", flush=True)
        time.sleep(sleep_for)
