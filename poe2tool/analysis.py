"""Investment analysis over the collected price history.

All prices are stored in Exalted; the 'divine' denomination divides each point
by the Divine price (in Exalted) at the nearest timestamp, so a rising
divine-denominated price literally means "beat holding Divines".

Per-item metrics
----------------
All metrics run on the item's *liquid sub-series*: points with quantity
>= max(2, 10% of the item's median quantity). Points backed by 1-2 listings
would otherwise define entries and peaks (day-one spikes are the worst case).
If fewer than MIN_POINTS survive the filter, the raw series is used.

entry_price   robust early base: median price of the first 48h of the
              (liquid) series.
peak          maximum of the smoothed series (rolling median, window 5) after
              the entry window start -> best_sell_ts / best_sell_price.
best_buy      minimum of the smoothed series at or before the peak (you must
              buy before you sell).
roi_peak      peak / entry - 1        (what you'd have made buying early)
roi_now       current / entry - 1     (what you'd have if you never sold)
roi_buy_sell  best_sell / best_buy - 1 (the realized optimum window)
max_drawdown  worst peak-to-trough drop of the smoothed series (<= 0).
volatility    std deviation of log returns between consecutive points.
liquidity     median listing quantity; illiquid = liquidity < 10 or < 24 points.

investment_score (the ranking number)
-------------------------------------
    score = ln(1 + max(min(roi_peak, roi_buy_sell), 0)) * liq_weight * time_weight
    liq_weight  = min(1, sqrt(liquidity / 20))   # full weight from ~20 listings
    time_weight = 1 / (1 + days_to_peak / 14)    # halves every 14 days entry->peak
    illiquid items: score * 0.1                  # flagged, never a top pick

min(roi_peak, roi_buy_sell) is the ROI that was actually actionable: a peak
that happens before you could buy (roi_buy_sell ~ 0) scores ~0, and a lucky
dip-buy below the entry median doesn't inflate the score beyond roi_peak.
High score = big actionable rise (log-damped so one 100x freak doesn't drown
everything), reachable market depth, and the rise happened fast.
"""

from __future__ import annotations

import math
import os
import sqlite3

import numpy as np
import pandas as pd

from . import db

ENTRY_WINDOW_HOURS = 48
MIN_POINTS = 8
ILLIQUID_MEDIAN_QTY = 10
ILLIQUID_MIN_POINTS = 24


def load_points(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT item_id, ts, price_exalted, quantity FROM price_points "
        "WHERE price_exalted > 0 ORDER BY item_id, ts",
        con,
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_items(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT item_id, name, category, api_id, type FROM items", con
    )


def reference_series(points: pd.DataFrame, items: pd.DataFrame, api_id: str) -> pd.DataFrame | None:
    """Price series (in Exalted) of a reference currency, e.g. divine/chaos."""
    match = items[items["api_id"].str.lower() == api_id.lower()].dropna(subset=["api_id"])
    if match.empty:
        match = items[items["name"].str.lower() == {"divine": "divine orb",
                                                    "chaos": "chaos orb"}.get(api_id, api_id)]
    if match.empty:
        return None
    ref_id = int(match.iloc[0]["item_id"])
    ref = points[points["item_id"] == ref_id][["ts", "price_exalted"]]
    if ref.empty:
        return None
    return ref.rename(columns={"price_exalted": "ref_price"}).sort_values("ts")


def to_denom(item_df: pd.DataFrame, ref: pd.DataFrame | None) -> pd.DataFrame:
    """Convert an item's exalted series into the reference denomination
    (price / ref_price at the nearest timestamp). ref=None -> exalted as-is."""
    out = item_df.sort_values("ts").copy()
    if ref is None:
        out["price"] = out["price_exalted"]
        return out
    out = pd.merge_asof(out, ref, on="ts", direction="nearest")
    out["price"] = out["price_exalted"] / out["ref_price"]
    return out.dropna(subset=["price"])


def analyze_item(g: pd.DataFrame) -> dict | None:
    g = g.sort_values("ts").reset_index(drop=True)
    if len(g) < MIN_POINTS:
        return None

    med_qty = float(g["quantity"].median())
    liquidity = med_qty
    illiquid = med_qty < ILLIQUID_MEDIAN_QTY or len(g) < ILLIQUID_MIN_POINTS

    # Work on the liquid sub-series: points backed by only 1-2 listings would
    # otherwise define entry AND peaks (day-one spikes are the worst offenders).
    min_qty = max(2.0, 0.1 * med_qty)
    liquid = g[g["quantity"] >= min_qty].reset_index(drop=True)
    if len(liquid) >= MIN_POINTS:
        g = liquid

    # --- entry: robust early base ---------------------------------------
    start = g["ts"].iloc[0]
    window = g[g["ts"] <= start + pd.Timedelta(hours=ENTRY_WINDOW_HOURS)]
    entry_price = float(window["price"].median())
    if entry_price <= 0 or not math.isfinite(entry_price):
        return None
    entry_ts = window["ts"].iloc[0]

    # --- smoothed series for peak / buy / drawdown ----------------------
    g["smooth"] = g["price"].rolling(5, center=True, min_periods=1).median()

    peak_idx = int(g["smooth"].idxmax())
    peak_price = float(g["smooth"].iloc[peak_idx])
    peak_ts = g["ts"].iloc[peak_idx]

    buy_slice = g.iloc[: peak_idx + 1]
    buy_idx = int(buy_slice["smooth"].idxmin())
    best_buy_price = float(g["smooth"].iloc[buy_idx])
    best_buy_ts = g["ts"].iloc[buy_idx]

    current_price = float(g["price"].tail(5).median())

    roi_peak = peak_price / entry_price - 1
    roi_now = current_price / entry_price - 1
    roi_buy_sell = (peak_price / best_buy_price - 1) if best_buy_price > 0 else 0.0

    cummax = g["smooth"].cummax()
    max_drawdown = float((g["smooth"] / cummax - 1).min())

    log_ret = np.log(g["price"]).diff().replace([np.inf, -np.inf], np.nan).dropna()
    volatility = float(log_ret.std()) if len(log_ret) > 2 else 0.0

    days_to_peak = max((peak_ts - entry_ts).total_seconds() / 86400.0, 0.0)
    liq_weight = min(1.0, math.sqrt(max(liquidity, 0.0) / 20.0))
    time_weight = 1.0 / (1.0 + days_to_peak / 14.0)
    # actionable ROI: you must be able to buy BEFORE the peak, so a peak that
    # is the first point of the series (roi_buy_sell = 0) scores ~0 even if
    # roi_peak measured from the entry median looks spectacular
    actionable_roi = min(roi_peak, roi_buy_sell)
    score = math.log1p(max(actionable_roi, 0.0)) * liq_weight * time_weight
    if illiquid:
        score *= 0.1

    return {
        "entry_ts": entry_ts.isoformat(), "entry_price": entry_price,
        "peak_ts": peak_ts.isoformat(), "peak_price": peak_price,
        "current_price": current_price,
        "roi_peak": roi_peak, "roi_now": roi_now,
        "best_buy_ts": best_buy_ts.isoformat(), "best_buy_price": best_buy_price,
        "best_sell_ts": peak_ts.isoformat(), "best_sell_price": peak_price,
        "roi_buy_sell": roi_buy_sell,
        "max_drawdown": max_drawdown, "volatility": volatility,
        "liquidity": liquidity, "illiquid": int(illiquid),
        "n_points": len(g), "investment_score": score,
    }


def run_analysis(db_path: str = db.DEFAULT_DB, csv_path: str | None = None,
                 top: int = 30) -> pd.DataFrame:
    con = db.connect(db_path)
    points = load_points(con)
    items = load_items(con)
    if points.empty:
        con.close()
        raise SystemExit("No price points in the DB yet - run `collect` first.")

    divine = reference_series(points, items, "divine")
    chaos = reference_series(points, items, "chaos")
    if divine is None:
        print("WARNING: no Divine history in the DB (partial collect?). "
              "The divine denomination will be skipped.")
    if chaos is None:
        print("WARNING: no Chaos history in the DB (partial collect?). "
              "The chaos denomination will be skipped.")

    denoms = {"exalted": None}
    if divine is not None:
        denoms["divine"] = divine
    if chaos is not None:
        denoms["chaos"] = chaos

    all_rows = []
    for denom, ref in denoms.items():
        for item_id, g in points.groupby("item_id"):
            converted = to_denom(g, ref)
            stats = analyze_item(converted)
            if stats is None:
                continue
            all_rows.append({"item_id": int(item_id), "denom": denom, **stats})

    con.execute("DELETE FROM analysis_results")
    cols = ["item_id", "denom", "entry_ts", "entry_price", "peak_ts", "peak_price",
            "current_price", "roi_peak", "roi_now", "best_buy_ts", "best_buy_price",
            "best_sell_ts", "best_sell_price", "roi_buy_sell", "max_drawdown",
            "volatility", "liquidity", "illiquid", "n_points", "investment_score"]
    con.executemany(
        f"INSERT OR REPLACE INTO analysis_results({','.join(cols)}) "
        f"VALUES({','.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in all_rows],
    )
    con.commit()

    result = pd.DataFrame(all_rows).merge(items, on="item_id")
    report_denom = "divine" if divine is not None else "exalted"
    report = (
        result[result["denom"] == report_denom]
        .sort_values("investment_score", ascending=False)
        .reset_index(drop=True)
    )

    out_csv = csv_path or os.path.join(
        os.path.dirname(os.path.abspath(db_path)), f"best_investments_{report_denom}.csv"
    )
    export_cols = ["name", "category", "entry_price", "entry_ts", "best_buy_price",
                   "best_buy_ts", "best_sell_price", "best_sell_ts", "peak_price",
                   "peak_ts", "current_price", "roi_peak", "roi_now", "roi_buy_sell",
                   "max_drawdown", "volatility", "liquidity", "illiquid",
                   "investment_score"]
    report[export_cols].to_csv(out_csv, index=False)

    # --- console report --------------------------------------------------
    print(f"\n=== BEST INVESTMENTS (denom: {report_denom}, liquid only) ===")
    liquid = report[report["illiquid"] == 0].head(top)
    print(f"{'Item':34} {'entry':>10} {'buy@':>10} {'sell@':>10} "
          f"{'ROI peak':>9} {'ROI now':>9} {'score':>7}  buy date -> sell date")
    for _, r in liquid.iterrows():
        print(f"{r['name'][:34]:34} {r['entry_price']:>10.4g} "
              f"{r['best_buy_price']:>10.4g} {r['best_sell_price']:>10.4g} "
              f"{r['roi_peak']*100:>8.1f}% {r['roi_now']*100:>8.1f}% "
              f"{r['investment_score']:>7.3f}  "
              f"{r['best_buy_ts'][:10]} -> {r['best_sell_ts'][:10]}")

    cats = (
        report.groupby("category")
        .agg(items=("item_id", "count"),
             median_roi_peak=("roi_peak", "median"),
             median_roi_now=("roi_now", "median"),
             pct_up=("roi_now", lambda s: float((s > 0).mean()) * 100))
        .sort_values("median_roi_now", ascending=False)
    )
    print(f"\n=== CATEGORIES vs {report_denom} (median ROI now / peak, % items up) ===")
    for cat, r in cats.iterrows():
        print(f"{str(cat):22} n={int(r['items']):4d}  "
              f"now {r['median_roi_now']*100:>+7.1f}%  "
              f"peak {r['median_roi_peak']*100:>+7.1f}%  up {r['pct_up']:>5.1f}%")

    print(f"\nSaved: {out_csv} ({len(report)} items), "
          f"analysis_results table refreshed ({len(all_rows)} rows).")
    db.checkpoint(con)
    con.close()
    return result
