#!/usr/bin/env python3
"""
poe2_movers.py - Zieht die KOMPLETTE Preis-Historie aller Items/Currencies aus der
poe2scout-API (dieselbe Datenquelle wie poe.ninja/poe2scout) und findet die Items,
die frueh guenstig waren und stark gestiegen sind ("early-cheap moonshots").

Reverse-engineered aus dem offiziellen poe2scout Backend + Frontend
(github.com/poe2scout/poe2scout). Reine Standardbibliothek - kein pip noetig.

Benutzung:
    python3 poe2_movers.py
    python3 poe2_movers.py --ref divine          # Preise in Divine (default) -> "schlaegt es Divine?"
    python3 poe2_movers.py --ref exalted          # Preise in Exalted (Rohwert)
    python3 poe2_movers.py --min-early 0.5        # nur Items, die frueh >= 0.5 (in ref) kosteten
    python3 poe2_movers.py --email you@mail.com   # die API bittet um Kontakt im User-Agent

Output:
    - poe2_movers.csv  (alle Items, sortiert nach Anstieg bis zum Peak)
    - Top-Liste in der Konsole
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.poe2scout.com"
REALM = "poe2"               # default realm path (PC)
SLEEP = 0.6                  # Sekunden zwischen Calls; API-Limit ~100/min, sei hoeflich
LOG_COUNT = 1000             # Punkte pro History-Call (MUSS Vielfaches von 4 sein)
MAX_PAGES = 6                # max History-Seiten pro Item (deckt eine ganze League ab)


def _norm(key: str) -> str:
    """Key auf reine Kleinbuchstaben/Ziffern reduzieren - macht uns unabhaengig von
    snake_case / camelCase / PascalCase im API-Output."""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def field(obj: dict, *names, default=None):
    """Holt ein Feld unabhaengig von der Schreibweise (z.B. item_id / itemId / ItemId)."""
    table = {_norm(k): v for k, v in obj.items()}
    for n in names:
        if _norm(n) in table:
            return table[_norm(n)]
    return default


def api_get(path: str, params: dict | None = None, email: str = "anon@example.com"):
    url = f"{BASE}/{path.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"poe2-movers-script (contact: {email})",
                 "Accept": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:                      # rate limited -> warten
                time.sleep(2 + attempt * 2)
                continue
            if e.code in (400, 404):
                return None                        # ungueltiges Item/keine Daten
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1 + attempt)
    return None


def pick_current_league(email: str) -> tuple[str, str]:
    leagues = api_get(f"{REALM}/Leagues", email=email)
    if not leagues:
        sys.exit("Konnte Liga-Liste nicht laden. API erreichbar? (api.poe2scout.com)")
    for lg in leagues:
        if field(lg, "is_current", "current_league", "isCurrent", default=False):
            return field(lg, "value"), field(lg, "short_name", "shortName", default="")
    # Fallback: erste Liga
    return field(leagues[0], "value"), field(leagues[0], "short_name", default="")


def fetch_history(league: str, item_id: int, ref: str | None, email: str) -> list[dict]:
    """Volle Historie eines Items als Liste von {price, time}. Paginiert rueckwaerts."""
    points: list[dict] = []
    end_time = None
    for _ in range(MAX_PAGES):
        data = api_get(
            f"{REALM}/Leagues/{urllib.parse.quote(league)}/Items/{item_id}/History",
            {"LogCount": LOG_COUNT, "ReferenceCurrency": ref, "EndTime": end_time},
            email=email,
        )
        if not data:
            break
        hist = field(data, "price_history", "priceHistory", default=[]) or []
        for p in hist:
            t = field(p, "time")
            price = field(p, "price")
            if t is None or price is None:
                continue
            points.append({"time": t, "price": float(price)})
        if not field(data, "has_more", "hasMore", default=False) or not hist:
            break
        # aelteste Zeit als naechster Endpunkt
        end_time = min(field(p, "time") for p in hist)
        time.sleep(SLEEP)
    # dedupe + sortieren nach Zeit
    seen, out = set(), []
    for p in sorted(points, key=lambda x: x["time"]):
        if p["time"] in seen:
            continue
        seen.add(p["time"])
        out.append(p)
    return out


def analyze(points: list[dict]) -> dict | None:
    if len(points) < 3:
        return None
    n_early = max(1, len(points) // 10)            # erste ~10% = "early"
    early = sorted(p["price"] for p in points[:n_early])
    early_price = early[len(early) // 2]           # median der early-Phase
    if early_price <= 0:
        return None
    peak = max(points, key=lambda p: p["price"])
    cur = points[-1]["price"]
    return {
        "early_date": points[0]["time"][:10],
        "early_price": round(early_price, 4),
        "peak_date": peak["time"][:10],
        "peak_price": round(peak["price"], 4),
        "current_price": round(cur, 4),
        "gain_to_peak_pct": round((peak["price"] / early_price - 1) * 100, 1),
        "gain_to_now_pct": round((cur / early_price - 1) * 100, 1),
        "n_points": len(points),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="divine",
                    help="Referenz-Currency fuer die Preise (divine/exalted/chaos). "
                         "divine -> du siehst direkt, was Divine schlaegt.")
    ap.add_argument("--min-early", type=float, default=0.0,
                    help="nur Items behalten, die frueh mind. so viel (in ref) kosteten")
    ap.add_argument("--limit", type=int, default=0,
                    help="nur die ersten N Items abfragen (zum Testen)")
    ap.add_argument("--email", default="anon@example.com",
                    help="Kontakt-Mail fuer den User-Agent (von der API erbeten)")
    ap.add_argument("--out", default="poe2_movers.csv")
    args = ap.parse_args()

    ref = None if args.ref.lower() in ("base", "exalted", "ex") else args.ref.lower()
    league, short = pick_current_league(args.email)
    print(f"Liga: {league} ({short}) | Referenz: {args.ref}")

    items = api_get(f"{REALM}/Leagues/{urllib.parse.quote(league)}/Items", email=args.email)
    if not items:
        sys.exit("Konnte Item-Liste nicht laden.")
    if args.limit:
        items = items[: args.limit]
    print(f"{len(items)} Items gefunden. Ziehe Historie (~{SLEEP}s/Call, dauert ein paar Minuten)...")

    rows = []
    for i, it in enumerate(items, 1):
        item_id = field(it, "item_id", "itemId", "currencyItemId", "uniqueItemId", "id")
        name = field(it, "name") or field(it, "text") or field(it, "api_id") or str(item_id)
        cat = field(it, "category_api_id", "categoryApiId", default="")
        if item_id is None:
            continue
        pts = fetch_history(league, int(item_id), ref, args.email)
        stats = analyze(pts)
        time.sleep(SLEEP)
        if not stats or stats["early_price"] < args.min_early:
            if i % 25 == 0:
                print(f"  ...{i}/{len(items)}")
            continue
        rows.append({"name": name, "category": cat, "item_id": item_id, **stats})
        if i % 25 == 0:
            print(f"  ...{i}/{len(items)}")

    rows.sort(key=lambda r: r["gain_to_peak_pct"], reverse=True)

    cols = ["name", "category", "item_id", "early_date", "early_price",
            "peak_date", "peak_price", "current_price",
            "gain_to_peak_pct", "gain_to_now_pct", "n_points"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"\nGespeichert: {args.out} ({len(rows)} Items)\n")
    print(f"=== TOP 25 ANSTIEGE (early -> peak, in {args.ref}) ===")
    print(f"{'Item':32} {'early':>9} {'peak':>9} {'->peak%':>9} {'peak-datum':>11}")
    for r in rows[:25]:
        print(f"{r['name'][:32]:32} {r['early_price']:>9} {r['peak_price']:>9} "
              f"{r['gain_to_peak_pct']:>8}% {r['peak_date']:>11}")


if __name__ == "__main__":
    main()
