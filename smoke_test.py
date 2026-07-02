#!/usr/bin/env python3
"""Smoke test against the live poe2scout API. Run this before/after changes to
poe2tool/api.py to confirm the API still answers and the data shape matches
what the collector expects.

    python smoke_test.py [--email you@mail.com]
"""

import argparse
import sys

from poe2tool.api import Client, field


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="alex.emty@gmail.com")
    args = ap.parse_args()
    client = Client(email=args.email)

    # 1. Leagues
    league = client.current_league()
    value = field(league, "value")
    print(f"[1] current league: value={value!r} "
          f"(is_current={field(league, 'is_current', 'isCurrent')})")
    assert value, "league has no 'value' field"

    # 2. Items
    items = client.items(value)
    print(f"[2] items endpoint: {len(items)} items")
    assert len(items) > 100, "suspiciously few items"
    sample = items[0]
    print(f"    sample keys: {sorted(sample.keys())}")
    for it in items[:3]:
        print(f"    - id={field(it, 'item_id', 'itemId', 'id')} "
              f"name={field(it, 'name') or field(it, 'text')!r} "
              f"cat={field(it, 'category_api_id', 'categoryApiId')!r} "
              f"api_id={field(it, 'api_id', 'apiId')!r}")

    # 3. History for known items (small LogCount, must be divisible by 4)
    wanted = {"mirror of kalandra", "hinekora's lock", "divine orb"}
    picked = []
    for it in items:
        name = (field(it, "name") or field(it, "text") or "").lower()
        if name in wanted:
            picked.append(it)
    if not picked:
        print("!! none of the known test items found; falling back to first 3 items")
        picked = items[:3]

    for it in picked:
        item_id = field(it, "item_id", "itemId", "id")
        name = field(it, "name") or field(it, "text")
        points, has_more = client.history_page(value, int(item_id), log_count=16)
        print(f"[3] history {name!r} (id={item_id}): {len(points)} points, "
              f"has_more={has_more}")
        assert points, f"no history points for {name}"
        p = points[-1]
        print(f"    latest: ts={p['ts']} price={p['price']} qty={p['quantity']}")
        assert p["price"] > 0 and p["ts"].endswith("+00:00")

    # 4. Pagination: EndTime = oldest ts of previous page must return older data
    item = picked[0]
    item_id = int(field(item, "item_id", "itemId", "id"))
    page1, has_more = client.history_page(value, item_id, log_count=16)
    if has_more:
        oldest = page1[0]["ts"]
        page2, _ = client.history_page(value, item_id, end_time=oldest, log_count=16)
        assert page2, "pagination returned no older points despite has_more=true"
        assert page2[-1]["ts"] <= oldest, "pagination did not go backwards in time"
        print(f"[4] pagination ok: page2 range {page2[0]['ts']} .. {page2[-1]['ts']} "
              f"(all <= {oldest})")
    else:
        print("[4] pagination skipped (has_more=false on first small page)")

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
