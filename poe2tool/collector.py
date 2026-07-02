"""Collector: full backfill + incremental updates of poe2scout price history.

Per-item state machine (sync_state.backfilled):
  0 / missing -> full backfill: paginate from now backwards until has_more=false.
                 Only marked backfilled=1 after the last page landed, so an
                 aborted run simply redoes that one item (inserts are idempotent).
  1           -> incremental: paginate from now backwards, stop as soon as a page
                 overlaps the newest stored timestamp.

New items that appear in /Items on later runs have no sync_state row and get a
full backfill automatically.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import db
from .api import LOG_COUNT, Client, field

MAX_PAGES_PER_ITEM = 60  # hard safety cap: 60 * 1000 hourly points ≈ 6.8 years


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_item(raw: dict) -> dict | None:
    item_id = field(raw, "item_id", "itemId", "id")
    if item_id is None:
        return None
    name = field(raw, "name") or field(raw, "text") or str(item_id)
    return {
        "item_id": int(item_id),
        "name": str(name),
        "category": field(raw, "category_api_id", "categoryApiId"),
        "api_id": field(raw, "api_id", "apiId"),
        "type": field(raw, "type"),
        "icon_url": field(raw, "icon_url", "iconUrl"),
        "current_price_exalted": field(raw, "current_price", "currentPrice"),
    }


def _backfill_item(con, client: Client, league: str, item_id: int) -> int:
    """Full history, paginating backwards. Returns number of new rows."""
    new_rows = 0
    end_time = None
    for _ in range(MAX_PAGES_PER_ITEM):
        points, has_more = client.history_page(league, item_id, end_time=end_time)
        if not points:
            break
        new_rows += db.insert_points(con, item_id, points)
        con.commit()
        if not has_more:
            break
        end_time = points[0]["ts"]  # oldest of this page -> next page is older
    return new_rows


def _update_item(con, client: Client, league: str, item_id: int) -> int:
    """Only new points since the newest stored ts. Returns number of new rows."""
    newest = db.max_ts(con, item_id)
    if newest is None:
        return _backfill_item(con, client, league, item_id)
    new_rows = 0
    end_time = None
    for _ in range(MAX_PAGES_PER_ITEM):
        points, has_more = client.history_page(league, item_id, end_time=end_time)
        if not points:
            break
        fresh = [p for p in points if p["ts"] > newest]
        new_rows += db.insert_points(con, item_id, fresh)
        con.commit()
        # oldest point of the page reaches into stored data -> done
        if points[0]["ts"] <= newest or not has_more:
            break
        end_time = points[0]["ts"]
    return new_rows


def collect(
    db_path: str,
    email: str,
    update: bool = False,
    limit: int | None = None,
) -> None:
    """`update` only changes intent messaging; the per-item sync state decides
    whether an item is backfilled or incrementally updated either way."""
    client = Client(email=email)
    con = db.connect(db_path)

    league_raw = client.current_league()
    league = field(league_raw, "value")
    short = field(league_raw, "short_name", "shortName", default="") or ""
    db.upsert_league(con, league, short, True)
    db.set_meta(con, "league", league)
    print(f"League: {league} ({short})")

    raw_items = client.items(league)
    items = [it for it in (_normalize_item(r) for r in raw_items) if it]
    if limit:
        items = items[:limit]
    for it in items:
        db.upsert_item(con, it)
    con.commit()
    mode = "update" if update else "collect"
    print(f"{len(items)} items ({mode} mode). Rate limit ~100 req/min, "
          f"a full backfill takes a while ...")

    total_new = 0
    started = time.monotonic()
    for i, it in enumerate(items, 1):
        item_id = it["item_id"]
        state = db.get_sync_state(con, item_id)
        if state and state["backfilled"]:
            new_rows = _update_item(con, client, league, item_id)
            action = "update"
        else:
            new_rows = _backfill_item(con, client, league, item_id)
            action = "backfill"
        db.set_sync_state(con, item_id, backfilled=True, last_synced=_now_iso())
        con.commit()
        total_new += new_rows
        elapsed = time.monotonic() - started
        rate = i / elapsed * 60 if elapsed > 0 else 0
        print(f"[{i}/{len(items)}] {it['name'][:40]:40} +{new_rows:5d} points "
              f"({action}) | total +{total_new} | {rate:.0f} items/min",
              flush=True)

    db.set_meta(con, "last_sync", _now_iso())
    if not update and not limit:
        db.set_meta(con, "last_full_sync", _now_iso())
    con.commit()
    db.checkpoint(con)
    con.close()
    print(f"\nDone: {total_new} new price points.")
