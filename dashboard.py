"""PoE2 Economy Investment Dashboard (Streamlit).

Start:  streamlit run dashboard.py     (or: python -m poe2tool dashboard)
"""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from poe2tool import db as dbmod
from poe2tool.analysis import reference_series, run_analysis, to_denom

DB_PATH = os.environ.get("POE2_DB", dbmod.DEFAULT_DB)

st.set_page_config(page_title="PoE2 Investment Analyzer", layout="wide")

# Streamlit Cloud secrets (Settings -> Secrets) aren't OS env vars by default;
# resolve_email() in cli.py only looks at os.environ / .env, so mirror it in
# here once - keeps the "Jetzt Daten holen" button working on Cloud too.
# st.secrets raises (not just "empty") when no secrets.toml exists at all,
# which is the normal case for local runs.
if "CONTACT_EMAIL" not in os.environ:
    try:
        os.environ["CONTACT_EMAIL"] = st.secrets["CONTACT_EMAIL"]
    except (FileNotFoundError, KeyError, st.errors.StreamlitSecretNotFoundError):
        pass


def _db_mtime() -> float:
    try:
        return os.path.getmtime(DB_PATH)
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def load_core(mtime: float):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    items = pd.read_sql_query(
        "SELECT item_id, name, category, api_id, type FROM items", con)
    analysis = pd.read_sql_query("SELECT * FROM analysis_results", con)
    freshness = con.execute("SELECT MAX(ts) FROM price_points").fetchone()[0]
    league = con.execute("SELECT value FROM meta WHERE key='league'").fetchone()
    con.close()
    return items, analysis, freshness, (league[0] if league else "?")


@st.cache_data(show_spinner=False)
def load_item_points(mtime: float, item_id: int, denom: str) -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    pts = pd.read_sql_query(
        "SELECT item_id, ts, price_exalted, quantity FROM price_points "
        "WHERE item_id=? AND price_exalted > 0 ORDER BY ts", con, params=(item_id,))
    pts["ts"] = pd.to_datetime(pts["ts"], utc=True)
    ref = None
    if denom == "divine":
        items = pd.read_sql_query(
            "SELECT item_id, name, category, api_id, type FROM items", con)
        all_ref = pd.read_sql_query(
            "SELECT item_id, ts, price_exalted, quantity FROM price_points "
            "WHERE price_exalted > 0", con)
        all_ref["ts"] = pd.to_datetime(all_ref["ts"], utc=True)
        ref = reference_series(all_ref, items, "divine")
    con.close()
    return to_denom(pts, ref)


@st.cache_data(show_spinner=False)
def category_index(mtime: float, denom: str) -> pd.DataFrame:
    """Median normalized price per category over time (start of series = 1.0)."""
    con = sqlite3.connect(DB_PATH)
    pts = pd.read_sql_query(
        "SELECT item_id, ts, price_exalted, quantity FROM price_points "
        "WHERE price_exalted > 0", con)
    items = pd.read_sql_query(
        "SELECT item_id, name, category, api_id, type FROM items", con)
    con.close()
    pts["ts"] = pd.to_datetime(pts["ts"], utc=True)
    ref = reference_series(pts, items, "divine") if denom == "divine" else None

    # per item: normalized series on a common 6h grid, forward-filled, so the
    # per-bucket median is not distorted by items dropping in and out
    series = {}
    for item_id, g in pts.groupby("item_id"):
        conv = to_denom(g, ref).sort_values("ts")
        med_qty = conv["quantity"].median()
        liquid = conv[conv["quantity"] >= max(2, 0.1 * (med_qty or 0))]
        if len(liquid) >= 8:
            conv = liquid
        if len(conv) < 8:
            continue
        base = conv["price"].head(6).median()
        if not base or base <= 0:
            continue
        s = (conv.assign(bucket=conv["ts"].dt.floor("6h"))
             .groupby("bucket")["price"].median() / base)
        series[int(item_id)] = s
    if not series:
        return pd.DataFrame()
    wide = pd.DataFrame(series).sort_index().ffill()

    cat_map = items.set_index("item_id")["category"]
    records = []
    for cat in cat_map.dropna().unique():
        cols = [c for c in wide.columns if cat_map.get(c) == cat]
        if len(cols) < 3:
            continue
        sub = wide[cols]
        n_present = sub.notna().sum(axis=1)
        med = sub.median(axis=1)[n_present >= max(3, len(cols) // 2)]
        records.append(pd.DataFrame(
            {"category": cat, "bucket": med.index, "index": med.values,
             "n_items": len(cols)}))
    if not records:
        return pd.DataFrame()
    return pd.concat(records, ignore_index=True)


@st.cache_data(show_spinner=False)
def divine_rate_series(mtime: float) -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT p.ts, p.price_exalted AS ref_price FROM price_points p "
        "JOIN items i ON i.item_id = p.item_id WHERE i.api_id='divine' "
        "ORDER BY p.ts", con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


FLAG_UI = [("identified", "Identified"), ("corrupted", "Corrupted"),
           ("unrevealed", "Unrevealed")]
FLAG_OPTIONS = ["any", "yes", "no"]


def load_favorites() -> pd.DataFrame:
    con = dbmod.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT f.id, f.item_id, i.name, i.category, f.flag_identified, "
        "f.flag_corrupted, f.flag_unrevealed, f.trade_label, f.tracking "
        "FROM favorites f JOIN items i USING(item_id) ORDER BY f.added_ts", con)
    con.close()
    return df


def set_tracking(fav_id: int, on: bool) -> str:
    """Toggle hourly trade snapshots for one favorite: activates the linked
    trade search (creates it on first activation), or pauses it."""
    from poe2tool.trade import add_item_search, flag_label
    con = dbmod.connect(DB_PATH)
    row = con.execute(
        "SELECT f.*, i.name FROM favorites f JOIN items i USING(item_id) "
        "WHERE f.id=?", (fav_id,)).fetchone()
    if not row:
        con.close()
        return "Favorit nicht gefunden."
    flags = {k: row[f"flag_{k}"] for k in ("identified", "corrupted", "unrevealed")}
    label = row["trade_label"] or flag_label(row["name"], flags)
    con.execute("UPDATE favorites SET tracking=?, trade_label=? WHERE id=?",
                (int(on), label, fav_id))
    if not on:
        con.execute("UPDATE trade_searches SET active=0 WHERE label=?", (label,))
        con.commit()
        con.close()
        return f"Tracking pausiert: {label} (Snapshots bleiben erhalten)"
    exists = con.execute("SELECT 1 FROM trade_searches WHERE label=?",
                         (label,)).fetchone()
    if exists:
        con.execute("UPDATE trade_searches SET active=1 WHERE label=?", (label,))
    con.commit()
    con.close()
    if exists:
        return f"Tracking an: {label}"
    return add_item_search(DB_PATH, int(row["item_id"]), flags)


def get_item_favorites(item_id: int) -> list:
    """All flag variants this item is favorited with."""
    con = dbmod.connect(DB_PATH)
    rows = con.execute("SELECT * FROM favorites WHERE item_id=? ORDER BY id",
                       (item_id,)).fetchall()
    con.close()
    return rows


def add_favorite(item_id: int, flags: dict) -> str:
    """Star an item AND auto-create its hourly trade watch with the flags.
    One favorite per (item, flag combination) - unid and id can coexist."""
    from poe2tool.trade import add_item_search, flag_label
    con = dbmod.connect(DB_PATH)
    name = con.execute("SELECT name FROM items WHERE item_id=?",
                       (item_id,)).fetchone()["name"]
    label = flag_label(name, flags)
    con.execute(
        "INSERT INTO favorites(item_id, added_ts, flag_identified, "
        "flag_corrupted, flag_unrevealed, trade_label) "
        "VALUES(?, datetime('now'), ?, ?, ?, ?) "
        "ON CONFLICT(item_id, flag_identified, flag_corrupted, flag_unrevealed) "
        "DO UPDATE SET trade_label=excluded.trade_label",
        (item_id, flags["identified"], flags["corrupted"], flags["unrevealed"],
         label),
    )
    con.commit()
    con.close()
    return add_item_search(DB_PATH, item_id, flags)


def remove_favorite(fav_id: int) -> None:
    """Remove one flag variant and pause its trade watch (snapshots kept)."""
    con = dbmod.connect(DB_PATH)
    row = con.execute(
        "SELECT f.trade_label, i.name FROM favorites f "
        "JOIN items i USING(item_id) WHERE f.id=?", (fav_id,)).fetchone()
    if row:
        label = row["trade_label"] or f"{row['name']} [trade]"
        con.execute("UPDATE trade_searches SET active=0 WHERE label=?", (label,))
        con.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
        con.commit()
    con.close()


def flag_selects(container, key_prefix: str) -> dict:
    cols = container.columns(len(FLAG_UI))
    return {
        key: cols[i].selectbox(title, FLAG_OPTIONS, key=f"{key_prefix}_{key}",
                               format_func={"any": "Any", "yes": "Ja",
                                            "no": "Nein"}.get)
        for i, (key, title) in enumerate(FLAG_UI)
    }


def flags_text(fav) -> str:
    parts = []
    for key, title in FLAG_UI:
        v = fav[f"flag_{key}"]
        if v in ("yes", "no"):
            parts.append(f"{title}: {'Ja' if v == 'yes' else 'Nein'}")
    return ", ".join(parts) or "Any"


def load_watchlist(mtime: float):
    con = sqlite3.connect(DB_PATH)
    searches = pd.read_sql_query(
        "SELECT search_id, label, league, active FROM trade_searches", con)
    snaps = pd.read_sql_query(
        "SELECT search_id, ts, total, n_used, min_exalted, med10_exalted "
        "FROM trade_snapshots ORDER BY ts", con)
    con.close()
    snaps["ts"] = pd.to_datetime(snaps["ts"], utc=True)
    return searches, snaps


def fmt_price(denom: str):
    return st.column_config.NumberColumn(format="%.4g " + ("div" if denom == "divine" else "ex"))


def pct_col():
    return st.column_config.NumberColumn(format="percent", help="relativ zum Einstiegspreis")


# ---------------------------------------------------------------- sidebar --
mtime = _db_mtime()
if not os.path.exists(DB_PATH):
    st.error(f"Keine Datenbank unter `{DB_PATH}`. Erst sammeln: "
             "`python -m poe2tool collect`")
    st.stop()

items, analysis, freshness, league = load_core(mtime)

if analysis.empty:
    with st.spinner("Analyse-Tabelle leer - berechne Analyse ..."):
        run_analysis(DB_PATH)
    st.cache_data.clear()
    items, analysis, freshness, league = load_core(_db_mtime())

# --- navigation: row clicks / name links land on the Item-Detail page ------
# widget keys can't be written after the widget exists, so navigation targets
# are staged in _nav_target and applied here, before the radio is created
if "_nav_target" in st.session_state:
    target_page, target_item = st.session_state.pop("_nav_target")
    st.session_state["page"] = target_page
    if target_item is not None:
        st.session_state["detail_item_id"] = target_item

qp_item = st.query_params.get("item")
if qp_item and qp_item != str(st.session_state.get("_qp_item_handled")):
    try:
        st.session_state["_qp_item_handled"] = qp_item
        st.session_state["detail_item_id"] = int(qp_item)
        st.session_state["page"] = "Item-Detail"
    except ValueError:
        pass


def item_link_col(df: pd.DataFrame) -> pd.DataFrame:
    """Prepend a link column (?item=<id>&n=<name>) rendered as a blue link."""
    out = df.copy()
    out.insert(0, "item", "?item=" + out["item_id"].astype(int).astype(str)
               + "&n=" + out["name"].astype(str))
    return out


LINK_COL = st.column_config.LinkColumn("Item", display_text=r"[?&]n=(.*)$",
                                       help="Klick öffnet die Detail-Seite")


st.sidebar.title("PoE2 Investment Analyzer")
st.sidebar.caption(f"Liga: **{league}**")
page = st.sidebar.radio("Seite", ["Overview", "Item-Detail", "Best Investments",
                                  "Kategorien", "Watchlist"], key="page")

# Filter-Widgets (flt_*) über Seitenwechsel am Leben halten: Streamlit räumt
# Widget-State auf, sobald das Widget einen Run lang nicht gerendert wird;
# die Selbstzuweisung macht den Key app-owned und verhindert genau das.
for _k in [k for k in st.session_state if k.startswith("flt_")]:
    st.session_state[_k] = st.session_state[_k]

# merken, von welcher Seite man zuletzt kam -> Ziel des Zurück-Buttons
if page != "Item-Detail":
    st.session_state["_back_page"] = page
denom = st.sidebar.radio("Denominierung", ["divine", "exalted"], horizontal=True,
                         format_func=lambda d: d.capitalize())
unit = "div" if denom == "divine" else "ex"

st.sidebar.divider()
st.sidebar.caption(f"Neuester Datenpunkt: `{freshness or '-'}`")
st.sidebar.info("Frische Daten holen:\n```\npython -m poe2tool collect --update\n```")
if st.sidebar.button("Analyse neu berechnen"):
    with st.spinner("Analysiere ..."):
        run_analysis(DB_PATH)
    st.cache_data.clear()
    st.rerun()

a = analysis[analysis["denom"] == denom].merge(items, on="item_id")

# ---------------------------------------------------------------- pages ----

if page == "Overview":
    st.header("Alle Items")
    c1, c2, c3 = st.columns([2, 2, 1])
    cats = sorted(a["category"].dropna().unique())
    st.session_state.setdefault("flt_liquid", True)
    sel_cats = c1.multiselect("Kategorien", cats, key="flt_cats")
    search = c2.text_input("Suche", key="flt_search")
    liquid_only = c3.checkbox("nur liquide", key="flt_liquid")

    view = a.copy()
    if sel_cats:
        view = view[view["category"].isin(sel_cats)]
    if search:
        view = view[view["name"].str.contains(search, case=False, na=False)]
    if liquid_only:
        view = view[view["illiquid"] == 0]

    view = (view.sort_values("investment_score", ascending=False)
            .reset_index(drop=True))
    st.caption(f"{len(view)} Items - Preise in {denom.capitalize()} - "
               "Klick auf den Namen öffnet die Detail-Seite")
    st.dataframe(
        item_link_col(view)[["item", "category", "entry_price", "peak_price",
                             "current_price", "roi_peak", "roi_now", "liquidity",
                             "volatility", "investment_score"]],
        column_config={
            "item": LINK_COL,
            "entry_price": fmt_price(denom), "peak_price": fmt_price(denom),
            "current_price": fmt_price(denom),
            "roi_peak": pct_col(), "roi_now": pct_col(),
            "investment_score": st.column_config.NumberColumn(format="%.3f"),
        },
        use_container_width=True, hide_index=True, height=600,
    )

elif page == "Item-Detail":
    back_page = st.session_state.get("_back_page", "Overview")
    if st.button(f"← Zurück zu {back_page}"):
        st.query_params.clear()   # sonst zieht ?item=... beim Refresh zurück
        st.session_state["_nav_target"] = (back_page, None)
        st.rerun()
    st.header("Item-Detail")
    a_sorted = a.sort_values("investment_score", ascending=False).reset_index(drop=True)
    names = a_sorted["name"] + "  (" + a_sorted["category"].fillna("?") + ")"
    default_idx = 0
    wanted = st.session_state.get("detail_item_id")
    if wanted is not None:
        match = a_sorted.index[a_sorted["item_id"] == wanted]
        if len(match):
            default_idx = int(match[0])
    choice = st.selectbox("Item (sortiert nach Score)", names, index=default_idx)
    row = a_sorted.iloc[list(names).index(choice)]
    item_id = int(row["item_id"])
    st.session_state["detail_item_id"] = item_id

    pts = load_item_points(mtime, item_id, denom)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=pts["ts"], y=pts["quantity"], name="Volumen",
                         yaxis="y2", marker_color="rgba(120,120,160,0.3)"))
    fig.add_trace(go.Scatter(x=pts["ts"], y=pts["price"], name=f"Preis ({unit})",
                             mode="lines", line=dict(color="#2b8cbe", width=2)))
    for ts_col, price_col, label, color, symbol in [
        ("best_buy_ts", "best_buy_price", "Best Buy", "#31a354", "triangle-up"),
        ("best_sell_ts", "best_sell_price", "Best Sell (Peak)", "#de2d26", "triangle-down"),
    ]:
        fig.add_trace(go.Scatter(
            x=[pd.to_datetime(row[ts_col])], y=[row[price_col]], name=label,
            mode="markers+text", text=[label], textposition="top center",
            marker=dict(size=14, color=color, symbol=symbol)))
    fig.update_layout(
        height=520, yaxis=dict(title=f"Preis ({unit})"),
        yaxis2=dict(title="Volumen", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h"), margin=dict(t=30))
    st.plotly_chart(fig, use_container_width=True)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Einstieg", f"{row['entry_price']:.4g} {unit}",
              help=f"Median der ersten 48h - {row['entry_ts'][:10]}")
    m2.metric("Peak", f"{row['peak_price']:.4g} {unit}",
              f"{row['roi_peak']*100:+.1f}%")
    m3.metric("Aktuell", f"{row['current_price']:.4g} {unit}",
              f"{row['roi_now']*100:+.1f}%")
    m4.metric("Max Drawdown", f"{row['max_drawdown']*100:.1f}%")
    m5.metric("Liquidität (Median-Volumen)", f"{row['liquidity']:.0f}",
              "illiquide!" if row["illiquid"] else "ok",
              delta_color="inverse" if row["illiquid"] else "normal")
    st.caption(f"Optimales Fenster: kaufen am **{row['best_buy_ts'][:16]}** zu "
               f"{row['best_buy_price']:.4g} {unit}, verkaufen am "
               f"**{row['best_sell_ts'][:16]}** zu {row['best_sell_price']:.4g} {unit} "
               f"→ ROI **{row['roi_buy_sell']*100:+.1f}%**")

    st.divider()
    if "_fav_msg" in st.session_state:
        st.success(st.session_state.pop("_fav_msg"))
    item_favs = get_item_favorites(item_id)
    for fav in item_favs:
        f1, f2 = st.columns([4, 1])
        state = ("Tracking **an** (stündliche Snapshots)" if fav["tracking"]
                 else "Tracking aus - auf der Watchlist-Seite aktivierbar")
        f1.markdown(f"★ **In der Watchlist** ({flags_text(fav)}) - {state}")
        if f2.button("Entfernen", key=f"fav_rm_{fav['id']}"):
            remove_favorite(fav["id"])
            st.rerun()
    st.markdown(("**☆ Weitere Flag-Variante favorisieren** (z. B. unid und id "
                 "getrennt tracken):" if item_favs else
                 "**☆ Favorisieren** - Trade-Flags für die stündlichen "
                 "Snapshots (Default: Any):"))
    flags = flag_selects(st, f"detail_{item_id}")
    if st.button("☆ Favorisieren + stündliche Snapshots"):
        if any(all(f[f"flag_{k}"] == v for k, v in flags.items())
               for f in item_favs):
            st.warning("Diese Flag-Kombination ist für das Item schon in der "
                       "Watchlist.")
        else:
            with st.spinner("Lege Trade-Suche an ..."):
                st.session_state["_fav_msg"] = add_favorite(item_id, flags)
            st.rerun()

elif page == "Best Investments":
    st.header("Best Investments (Ranking)")
    st.caption("Sortiert nach `investment_score` - illiquide Items ausgeblendet. "
               "Score = ln(1 + realisierbarer ROI) x Liquiditäts-Gewicht x "
               "Zeit-Gewicht (schnellere Peaks zählen mehr); realisierbar = "
               "min(ROI Entry→Peak, ROI Best-Buy→Peak).")
    top = (a[a["illiquid"] == 0].sort_values("investment_score", ascending=False)
           .head(50).reset_index(drop=True))
    tbl = item_link_col(top)[["item", "category", "best_buy_ts", "best_buy_price",
                              "best_sell_ts", "best_sell_price", "roi_buy_sell",
                              "roi_peak", "roi_now", "liquidity",
                              "investment_score"]].copy()
    tbl["best_buy_ts"] = tbl["best_buy_ts"].str[:16]
    tbl["best_sell_ts"] = tbl["best_sell_ts"].str[:16]
    st.dataframe(
        tbl.rename(columns={"best_buy_ts": "Kaufen am", "best_sell_ts": "Verkaufen am",
                            "best_buy_price": "Kauf", "best_sell_price": "Verkauf"}),
        column_config={
            "item": LINK_COL,
            "Kauf": fmt_price(denom), "Verkauf": fmt_price(denom),
            "roi_buy_sell": pct_col(), "roi_peak": pct_col(), "roi_now": pct_col(),
            "investment_score": st.column_config.NumberColumn(format="%.3f"),
        },
        use_container_width=True, hide_index=True, height=650,
    )

elif page == "Kategorien":
    st.header("Kategorie-Entwicklung")
    agg = (a.groupby("category")
           .agg(items=("item_id", "count"),
                median_roi_peak=("roi_peak", "median"),
                median_roi_now=("roi_now", "median"),
                anteil_gestiegen=("roi_now", lambda s: float((s > 0).mean())))
           .sort_values("median_roi_now", ascending=False).reset_index())
    st.dataframe(agg, column_config={
        "median_roi_peak": pct_col(), "median_roi_now": pct_col(),
        "anteil_gestiegen": st.column_config.NumberColumn(format="percent"),
    }, use_container_width=True, hide_index=True)

    st.subheader(f"Preis-Index je Kategorie (Start = 1.0, in {denom.capitalize()})")
    with st.spinner("Berechne Kategorie-Index ..."):
        idx = category_index(mtime, denom)
    if not idx.empty:
        big = (idx.groupby("category")["n_items"].first()
               .sort_values(ascending=False).head(12).index)
        fig = go.Figure()
        for cat in big:
            sub = idx[idx["category"] == cat]
            fig.add_trace(go.Scatter(x=sub["bucket"], y=sub["index"],
                                     mode="lines", name=str(cat)))
        fig.add_hline(y=1.0, line_dash="dot", line_color="gray")
        fig.update_layout(height=520, yaxis_title="Median-Index (Start = 1.0)",
                          legend=dict(orientation="h"), margin=dict(t=30))
        st.plotly_chart(fig, use_container_width=True)
    st.caption("Index = Median über alle Items der Kategorie von "
               "(Preis / Preis am Serienstart), 6h-Buckets. "
               "Ein Index > 1 heißt: die Kategorie ist gegen "
               f"{denom.capitalize()} gestiegen.")

elif page == "Watchlist":
    st.header("Watchlist")

    # --- favorites: starred poe2scout items (full history already in the DB)
    st.subheader("Favoriten")
    if "_fav_msg" in st.session_state:
        st.success(st.session_state.pop("_fav_msg"))
    favs = load_favorites()
    with st.expander("☆ Item favorisieren (+ stündliche Trade-Snapshots)"):
        all_names = a.sort_values("name")["name"].tolist()
        pick = st.selectbox("Item", ["-"] + all_names)
        flags = flag_selects(st, "wl_add")
        if st.button("☆ Favorisieren") and pick != "-":
            pid = int(a.loc[a["name"] == pick, "item_id"].iloc[0])
            with st.spinner("Lege Trade-Suche an ..."):
                st.session_state["_fav_msg"] = add_favorite(pid, flags)
            st.rerun()

    if favs.empty:
        st.caption("Noch keine Favoriten - hier oder auf der Item-Detail-Seite "
                   "über ☆ hinzufügen.")
    else:
        favs["flags"] = favs.apply(flags_text, axis=1)
        ftab = favs.merge(
            a[["item_id", "current_price", "roi_now", "roi_peak",
               "liquidity"]], on="item_id", how="left").reset_index(drop=True)
        ftab["tracking"] = ftab["tracking"].astype(bool)
        show = item_link_col(ftab)[["item", "flags", "tracking", "category",
                                    "current_price", "roi_now", "roi_peak",
                                    "liquidity"]]
        st.session_state.setdefault("_fav_editor_ver", 0)
        edited = st.data_editor(
            show,
            column_config={
                "item": LINK_COL,
                "tracking": st.column_config.CheckboxColumn(
                    "Tracking", help="stündliche Trade-Snapshots für diesen "
                                     "Favoriten aufzeichnen"),
                "current_price": fmt_price(denom),
                "roi_now": pct_col(), "roi_peak": pct_col(),
            },
            disabled=["item", "flags", "category", "current_price", "roi_now",
                      "roi_peak", "liquidity"],
            use_container_width=True, hide_index=True,
            key=f"fav_editor_{st.session_state['_fav_editor_ver']}",
        )
        changed = edited["tracking"] != show["tracking"]
        if changed.any():
            msgs = []
            for idx in edited.index[changed]:
                msgs.append(set_tracking(int(ftab.loc[idx, "id"]),
                                         bool(edited.loc[idx, "tracking"])))
            st.session_state["_fav_msg"] = " | ".join(msgs)
            st.session_state["_fav_editor_ver"] += 1  # Editor-State zurücksetzen
            st.rerun()
        st.caption("Haken in **Tracking** an/aus = stündliche Trade-Snapshots "
                   "aktivieren/pausieren. Zur Detail-Seite über den Item-Link.")

        fig = go.Figure()
        for _, f in favs.drop_duplicates("item_id").iterrows():
            pts = load_item_points(mtime, int(f["item_id"]), denom)
            if pts.empty:
                continue
            base = pts["price"].head(6).median()
            if not base or base <= 0:
                continue
            fig.add_trace(go.Scatter(x=pts["ts"], y=pts["price"] / base,
                                     mode="lines", name=f["name"]))
        if fig.data:
            fig.add_hline(y=1.0, line_dash="dot", line_color="gray")
            fig.update_layout(height=400, yaxis_title="Preis (Start = 1.0)",
                              legend=dict(orientation="h"), margin=dict(t=30))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Normalisierte Entwicklung (Serienstart = 1.0) - absolute "
                       "Preise über den Item-Link.")

    # --- official-trade watches (own recorded history, e.g. unid items) -----
    st.subheader("Trade-Suchen (eigene Aufzeichnung)")
    if "_trade_msg" in st.session_state:
        st.success("  \n".join(st.session_state.pop("_trade_msg")))
    if st.button("Jetzt Daten holen",
                 help="Holt sofort einen Snapshot aller aktiven Suchen von der "
                      "offiziellen Trade-API - unabhängig vom stündlichen Task."):
        from poe2tool.cli import resolve_email
        from poe2tool.trade import collect_snapshots
        with st.spinner("Hole aktuelle Listings von der Trade-API "
                        "(~2 s pro Request wegen Rate-Limit) ..."):
            st.session_state["_trade_msg"] = collect_snapshots(
                DB_PATH, resolve_email(None))
        st.rerun()
    searches, snaps = load_watchlist(mtime)
    if searches.empty:
        st.info("Noch keine Suchen beobachtet. Hinzufügen:\n```\n"
                "python -m poe2tool trade add <trade-URL> --label \"Mein Item\"\n```")
        st.stop()

    snaps = snaps.merge(searches[["search_id", "label"]], on="search_id")
    if denom == "divine":
        div = divine_rate_series(mtime)
        snaps = pd.merge_asof(snaps.sort_values("ts"), div, on="ts",
                              direction="nearest")
        snaps["min_p"] = snaps["min_exalted"] / snaps["ref_price"]
        snaps["med10"] = snaps["med10_exalted"] / snaps["ref_price"]
    else:
        snaps["min_p"] = snaps["min_exalted"]
        snaps["med10"] = snaps["med10_exalted"]

    latest = (snaps.sort_values("ts").groupby("search_id").tail(1)
              [["label", "ts", "total", "min_p", "med10"]]
              .rename(columns={"total": "Listings", "min_p": "Min",
                               "med10": "Median (10 günstigste)"}))
    st.dataframe(latest, column_config={
        "Min": fmt_price(denom), "Median (10 günstigste)": fmt_price(denom),
    }, use_container_width=True, hide_index=True)

    sel = st.multiselect("Suchen im Chart", sorted(searches["label"].unique()),
                         default=sorted(searches["label"].unique()))
    fig = go.Figure()
    for label in sel:
        sub = snaps[snaps["label"] == label].sort_values("ts")
        fig.add_trace(go.Scatter(x=sub["ts"], y=sub["med10"], name=label,
                                 mode="lines+markers"))
        fig.add_trace(go.Scatter(x=sub["ts"], y=sub["min_p"],
                                 name=f"{label} (min)", mode="lines",
                                 line=dict(dash="dot", width=1),
                                 opacity=0.5, showlegend=False))
    fig.update_layout(height=500, yaxis_title=f"Preis ({unit})",
                      legend=dict(orientation="h"), margin=dict(t=30))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Durchgezogen = Median der 10 günstigsten Listings, "
               "gepunktet = günstigstes Listing. Ein Punkt pro Snapshot - "
               "die Historie entsteht ab jetzt durch den stündlichen Task "
               "(`trade_collect.bat`).")
