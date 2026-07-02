"""CLI entry point.

    python -m poe2tool collect              # full backfill on empty DB, resumable
    python -m poe2tool collect --update     # incremental: only new points
    python -m poe2tool collect --limit 20   # quick test run
    python -m poe2tool analyze              # compute metrics + ranking, export CSV
    python -m poe2tool dashboard            # start the Streamlit dashboard
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .db import DEFAULT_DB


def load_env(path: str = ".env") -> dict:
    """Tiny .env reader (KEY=VALUE lines) - avoids a python-dotenv dependency."""
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("'\"")
    return env


def resolve_email(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    env = load_env()
    return (
        env.get("CONTACT_EMAIL")
        or os.environ.get("CONTACT_EMAIL")
        or "anon@example.com"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="poe2tool", description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="fetch items + price history into SQLite")
    p_collect.add_argument("--update", action="store_true",
                           help="incremental run: only load points newer than the DB")
    p_collect.add_argument("--limit", type=int, default=None,
                           help="only the first N items (test/dry run)")
    p_collect.add_argument("--email", default=None,
                           help="contact email for the User-Agent (or CONTACT_EMAIL in .env)")

    p_analyze = sub.add_parser("analyze", help="compute investment metrics + ranking")
    p_analyze.add_argument("--csv", default=None,
                           help="also export the ranking to this CSV path "
                                "(default data/best_investments_<denom>.csv)")
    p_analyze.add_argument("--top", type=int, default=30,
                           help="how many rows to print (default 30)")

    sub.add_parser("dashboard", help="start the Streamlit dashboard")

    p_trade = sub.add_parser(
        "trade", help="watchlist: poll saved official-trade searches")
    trade_sub = p_trade.add_subparsers(dest="trade_cmd", required=True)
    t_add = trade_sub.add_parser("add", help="watch a saved trade search")
    t_add.add_argument("ref", help="trade URL or search id")
    t_add.add_argument("--label", default=None, help="display name (default: auto)")
    t_item = trade_sub.add_parser(
        "add-item", help="watch a poe2scout item by name (no trade URL needed)")
    t_item.add_argument("name", help="item name, e.g. \"Hinekora's Lock\"")
    for flag in ("identified", "corrupted", "unrevealed"):
        t_item.add_argument(f"--{flag}", choices=["any", "yes", "no"],
                            default="any")
    trade_sub.add_parser("list", help="show watched searches")
    t_rm = trade_sub.add_parser("remove", help="pause a watched search")
    t_rm.add_argument("ref", help="search id or label")
    t_col = trade_sub.add_parser("collect", help="take one snapshot per search")
    t_col.add_argument("--loop", type=int, default=None, metavar="SECONDS",
                       help="keep running, one snapshot round per interval")
    t_col.add_argument("--email", default=None,
                       help="contact email for the poe2scout rate refresh")

    args = ap.parse_args(argv)

    if args.cmd == "collect":
        from .collector import collect
        collect(args.db, email=resolve_email(args.email),
                update=args.update, limit=args.limit)
        return 0

    if args.cmd == "analyze":
        from .analysis import run_analysis
        run_analysis(args.db, csv_path=args.csv, top=args.top)
        return 0

    if args.cmd == "trade":
        from . import trade
        if args.trade_cmd == "add":
            trade.add_search(args.db, args.ref, args.label)
        elif args.trade_cmd == "add-item":
            import sqlite3
            from . import db as dbmod
            con = dbmod.connect(args.db)
            row = con.execute(
                "SELECT item_id, name FROM items WHERE name LIKE ? "
                "ORDER BY LENGTH(name) LIMIT 1", (f"%{args.name}%",)).fetchone()
            con.close()
            if not row:
                raise SystemExit(f"no item matching {args.name!r} in the DB")
            flags = {"identified": args.identified, "corrupted": args.corrupted,
                     "unrevealed": args.unrevealed}
            print(trade.add_item_search(args.db, row["item_id"], flags))
        elif args.trade_cmd == "list":
            trade.list_searches(args.db)
        elif args.trade_cmd == "remove":
            trade.remove_search(args.db, args.ref)
        elif args.trade_cmd == "collect":
            email = resolve_email(args.email)
            if args.loop:
                trade.collect_loop(args.db, email, args.loop)
            else:
                trade.collect_snapshots(args.db, email)
        return 0

    if args.cmd == "dashboard":
        app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "dashboard.py")
        return subprocess.call([sys.executable, "-m", "streamlit", "run", app])

    return 1


if __name__ == "__main__":
    sys.exit(main())
