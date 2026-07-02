# PoE2 Economy Investment Analyzer

Sammelt die komplette Preis-Historie aller Items/Currencies der aktuellen
Path-of-Exile-2-Liga aus der [poe2scout-API](https://api.poe2scout.com),
speichert sie lokal in SQLite und analysiert, **welche Items zu welchen
Zeitpunkten gute Investitionen gewesen wären** – inkl. Dashboard.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # CONTACT_EMAIL eintragen (kommt in den User-Agent)
```

## Befehle

| Befehl | Was passiert |
|---|---|
| `python -m poe2tool collect` | Voll-Backfill: alle Items + komplette Historie. **Resumierbar** – bei Abbruch einfach neu starten, fertige Items werden übersprungen. |
| `python -m poe2tool collect --update` | Inkrementell: nur Punkte, die neuer sind als die DB. Neue Items werden automatisch voll geladen. Mehrfach hintereinander ausführen ist harmlos (idempotent, ~0 neue Zeilen). |
| `python -m poe2tool collect --limit 20` | Nur die ersten 20 Items (schneller Test). |
| `python -m poe2tool analyze` | Metriken + Ranking berechnen → Tabelle `analysis_results` in der DB + `data/best_investments_divine.csv` + Konsolen-Report. |
| `python -m poe2tool dashboard` | Dashboard starten (alternativ `streamlit run dashboard.py`). |
| `python smoke_test.py` | Prüft, ob die API antwortet und die Datenform stimmt. |
| `python -m poe2tool trade add <trade-URL>` | Gespeicherte Trade-Suche zur Watchlist hinzufügen. |
| `python -m poe2tool trade add-item "<Item-Name>"` | Trade-Suche für ein poe2scout-Item automatisch anlegen (kein Link nötig). |
| `python -m poe2tool trade collect` | Ein Preis-Snapshot pro beobachteter Suche. |
| `python -m poe2tool trade list` / `trade remove <id>` | Watchlist verwalten. |

Alle Preise werden **in Exalted** gespeichert (Single Source of Truth) und bei
Analyse/Anzeige über die Divine-Historie in Divine umgerechnet:
`preis_divine(t) = preis_exalted(t) / divine_in_exalted(t)`.

Rate-Limit: der Collector bleibt unter ~100 Requests/min und macht bei
HTTP 429 exponentielles Backoff. Ein Voll-Backfill von ~1300 Items dauert
je nach Liga-Alter ca. 15–60 Minuten.

## Analyse: Was bedeuten die Zahlen?

Default-Denominierung ist **Divine** – ein steigender Preis heißt also
wörtlich „hat Divine geschlagen". Im Dashboard umschaltbar auf Exalted.

Alle Metriken laufen auf der **liquiden Teil-Serie**: nur Punkte mit
Volumen ≥ max(2, 10 % des Median-Volumens des Items). Punkte mit 1–2
Listings würden sonst Einstieg *und* Peaks definieren (Tag-1-Spikes sind
der schlimmste Fall).

- **entry_price** – robuster früher Basispreis: Median der ersten 48 h der
  (liquiden) Serie.
- **peak / best_sell** – Maximum der geglätteten Serie (rollender Median,
  Fenster 5), inkl. Zeitpunkt.
- **best_buy** – Minimum der geglätteten Serie **vor** dem Peak: der
  Zeitpunkt, zu dem man hätte kaufen müssen.
- **roi_peak** = Peak / Entry − 1 (früh gekauft, am Peak verkauft)
- **roi_now** = Aktuell / Entry − 1 (früh gekauft, nie verkauft)
- **roi_buy_sell** = Best Sell / Best Buy − 1 (das perfekte Fenster)
- **max_drawdown** – schlimmster Einbruch vom Zwischenhoch (≤ 0).
- **volatility** – Standardabweichung der Log-Returns zwischen den Punkten.
- **liquidity** – Median der Listing-Anzahl. Items mit Median < 10 oder
  < 24 Datenpunkten werden als **illiquide** geflaggt und tauchen nicht in
  den Top-Picks auf (1–2 Listings machen jeden „Preis" bedeutungslos).

### investment_score (die Rangzahl)

```
score = ln(1 + max(min(roi_peak, roi_buy_sell), 0)) × liq_weight × time_weight
liq_weight  = min(1, sqrt(liquidity / 20))   # volles Gewicht ab ~20 Listings
time_weight = 1 / (1 + tage_bis_peak / 14)   # halbiert sich je 14 Tage Entry→Peak
illiquide Items: score × 0.1
```

`min(roi_peak, roi_buy_sell)` ist der **tatsächlich realisierbare** ROI:
ein Peak, der vor dem ersten Kaufzeitpunkt lag (roi_buy_sell ≈ 0), bekommt
Score ≈ 0 – egal wie spektakulär roi_peak aussieht. Hoher Score = großer
realisierbarer Anstieg (logarithmisch gedämpft, damit ein einzelner
100×-Ausreißer nicht alles dominiert), bei echter Markttiefe, und der
Anstieg kam schnell (Kapital war kurz gebunden).

### Buy-/Sell-Fenster lesen

Im Ranking und im Item-Detail stehen `Kaufen am` (grüner Marker im Chart:
frühes Minimum vor dem Peak) und `Verkaufen am` (roter Marker: Peak).
Das ist **rückblickende** Analyse – „wäre gewesen", kein Signal für morgen.
Muster, die sich wiederholen (z. B. Kategorien, die jede Liga in Woche 2–4
gegen Divine steigen), erkennt man auf der **Kategorien**-Seite.

## Dashboard-Seiten

- **Overview** – alle Items, sortier-/filterbar (Kategorie, Suche, nur
  liquide), Toggle Divine ↔ Exalted in der Sidebar.
- **Item-Detail** – Preis-Chart mit Best-Buy-/Peak-Markern, Volumen als
  Overlay, Kennzahlen.
- **Best Investments** – Top 50 nach `investment_score` mit Kauf-/
  Verkaufsdatum und ROI.
- **Kategorien** – welche Kategorien systematisch gegen Divine gestiegen
  sind, plus Preis-Index je Kategorie über die Liga.

Für frische Daten: `python -m poe2tool collect --update`, dann im Dashboard
„Analyse neu berechnen" klicken (oder `python -m poe2tool analyze`).

## Watchlist: eigene Historie für Items ohne öffentliche Daten

poe2scout/poe.ninja aggregieren nur **identifizierte** Items – z. B. für
unidentifizierte Voices-Sapphires gibt es nirgendwo Historie. Die Watchlist
pollt dafür die **offizielle Trade-API** (funktioniert ohne Login; die
HTML-Seiten sind Cloudflare-geschützt, die API-Endpunkte nicht):

1. Suche im offiziellen Trade-Site bauen und speichern → URL kopieren.
2. `python -m poe2tool trade add "https://www.pathofexile.com/trade2/search/poe2/.../XXXX"`
   – die Query wird über die API geholt und gecacht, das Label automatisch
   erzeugt (z. B. `Voices Sapphire [unid]`).
3. `python -m poe2tool trade collect` – pro Suche ein Snapshot: Anzahl
   Listings, günstigstes Listing und **Median der 10 günstigsten** (robuster
   als das Minimum, da Unterpreis-Listings sofort weggekauft werden).
   Preise werden über die aktuellen poe2scout-Kurse nach Exalted
   normalisiert; die Referenzkurse (Divine/Chaos) werden bei jedem Snapshot
   mit aufgefrischt.
4. Dashboard-Seite **Watchlist**: Serien aller beobachteten Suchen,
   Divine/Exalted umschaltbar.

### Favoriten (ohne Trade-Link)

Für **normale Items** braucht es keinen Trade-Link – favorisieren geht
direkt auf der Item-Detail-Seite oder auf der Watchlist-Seite. Beim
Favorisieren wählt man drei **Trade-Flags** (Default: Any):

| Flag | Trade-API-Feld | wofür |
|---|---|---|
| Identified | `identified` | id vs. unid (z. B. Voices: unid ist ein Vielfaches wert) |
| Corrupted | `corrupted` | corrupted-only Rolls |
| Unrevealed | `veiled` | Items mit unaufgedeckten Mods (Heart of the Well, Diamonds …) |

**Favorisieren legt automatisch die stündliche Trade-Suche an** (Query aus
Name + Basetype + Flags, Status `securable` = sofort kaufbar inkl.
Offline-Verkäufer – mit `online` findet man bei teuren Items fast nichts).
Entfernen pausiert die Suche; aufgezeichnete Snapshots bleiben erhalten.

**Dasselbe Item kann mehrfach favorisiert werden**, solange die
Flag-Kombination unterschiedlich ist – z. B. *Prism of Belief* einmal mit
Identified=Nein und einmal mit Identified=Ja, um unid- und id-Preise als
getrennte Serien zu tracken. Auf der Item-Detail-Seite werden alle Varianten
untereinander angezeigt, jede mit eigenem Entfernen-Button.

**Tracking-Spalte:** In der Favoriten-Tabelle auf der Watchlist-Seite hat
jede Zeile eine **Tracking**-Checkbox (Default: an). Nur angehakte Favoriten
werden stündlich über die Trade-API aufgezeichnet – so kann man viele Items
als Favoriten sammeln, aber gezielt nur einige davon tracken. Haken raus =
Trade-Suche pausiert (Snapshots bleiben), Haken rein = reaktiviert bzw. beim
ersten Mal automatisch angelegt.
Gleiches per CLI:

```bash
python -m poe2tool trade add-item "Voices" --identified no --corrupted yes
python -m poe2tool trade add-item "Heart of the Well" --unrevealed yes
```

Uniques haben bei poe2scout nur ~2 Punkte/Tag – die stündlichen Snapshots
geben Favoriten deutlich feinere Auflösung.

Listings in exotischen Währungen (z. B. „99× Waystone") sind fast immer
Platzhalter-/Trollpreise und fließen **nicht** in die Snapshots ein – nur
Exalted/Divine/Chaos/Mirror zählen.

**Wichtig:** Historie entsteht erst ab Aufzeichnungsbeginn – rückwirkend
existieren diese Daten nirgendwo.

### Stündlich automatisch (Windows Task Scheduler)

Kein Docker nötig – `trade_collect.bat` wechselt selbst ins Projektverzeichnis
und loggt nach `data/trade_watch.log`. Einmalig registrieren:

```powershell
schtasks /Create /F /SC HOURLY /TN "PoE2 Watchlist" /TR '"C:\Users\alexe\Desktop\poe 2 tool\trade_collect.bat"'
```

Prüfen: `schtasks /Query /TN "PoE2 Watchlist"` · sofort testen:
`schtasks /Run /TN "PoE2 Watchlist"` · entfernen:
`schtasks /Delete /F /TN "PoE2 Watchlist"`.

Der Task läuft nur, solange der PC an ist – ist er aus, entsteht eine Lücke
(unvermeidbar, da die Trade-API keine Vergangenheit liefert). Rate-Limits
sind kein Thema: ein Snapshot braucht ~3 Requests pro Suche, erlaubt sind 30
Suchanfragen pro 5 Minuten; bei HTTP 429 wird `Retry-After` respektiert.

## Technik

- **SQLite** (`data/poe2.db`): `items`, `price_points` mit
  `UNIQUE(item_id, ts)` (Idempotenz), `sync_state` (pro Item: backfilled?
  → macht den Backfill resumierbar), `analysis_results`, `leagues`, `meta`.
- API-Fallstricke behandelt: `LogCount` durch 4 teilbar, League-Wert
  URL-encoded, Response-Keys case-insensitiv geparst (`field()`), Pagination
  über `EndTime` = älteste Zeit der letzten Seite solange `has_more`,
  Backoff bei 429/5xx/Timeouts.
- `poe2_movers.py` ist das ursprüngliche Referenz-Script (Standalone).
