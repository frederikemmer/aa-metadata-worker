# Datenmodell

## Tabellen

### metadata_records

Primäre Entität ist der **Metadata Record** (= ein konkreter Source-/File-Record,
identifiziert durch MD5). MD5 ≠ logisches Buch: FE.Library gruppiert später
selbst über `work_key`.

| Spalte | Typ | Inhalt |
|---|---|---|
| `md5` | `BYTEA(16)` PK | Roher MD5 (platzsparend; API liefert Hex) |
| `title` / `title_norm` | TEXT | Original + normalisiert (NFKD, casefold, ohne Diakritika) |
| `authors` | TEXT[] | Autorenliste (Originalschreibweise) |
| `author_tokens` | TEXT[] | Normalisierte Einzelwörter aller Autoren (für Autor-Filter via GIN-Containment) |
| `publisher` | TEXT | Verlag |
| `publication_year` | SMALLINT | 1000–2100 |
| `languages` | TEXT[] | ISO-639-3 (FE.Library-Konvention), z. B. `deu` |
| `extension` / `filesize` | TEXT/BIGINT | Format (lowercase, validiert) und Dateigröße in Bytes |
| `isbn10`, `isbn13`, `doi`, `oclc`, `openlibrary_ids` | TEXT[] | Normalisierte, checksummengeprüfte Identifier (Arrays, da Quellen Mehrfachnennungen liefern) |
| `work_key` | TEXT NULL | Deterministische logische Werk-ID (siehe unten) |
| `series_name` | TEXT | Reihenname (z.B. `"Wicked Games"`) |
| `series_position` | SMALLINT | Bandnummer in der Reihe |
| `edition` | TEXT | Auflage (z.B. `"2nd Edition"`, `"Reissue"`) |
| `source_collection` | TEXT | `zlib3_records` \| `ia2_records` \| `upload_records` \| `goodreads_records` \| `gbooks_records` \| `libby_records` |
| `source_record_id` | TEXT | z. B. `zlibrary_id`, `ia_id`, `primary_id` |
| `aacid` | TEXT | AACID des letzten importierenden Records (Provenance) |
| `source_timestamp` | TIMESTAMPTZ | Scrape-Timestamp aus dem AACID |
| `quality_score` | SMALLINT | Deterministischer Vollständigkeits-Score (Merge-Kriterium) |
| `deleted`, `removed_reason` | BOOL/TEXT | Tombstones (`removed` bei zlib, `deleted_as_duplicate` bei uploads) |
| `ipfs_cid` | TEXT | Stabile Inhaltsreferenz, wenn die Quelle sie mitliefert (für `/records/{md5}/sources`) |
| `search_tsv` | TSVECTOR | Gewichtetes FTS-Feld: A=Titel, B=Autor-Token, C=Verlag (Trigger gepflegt) |
| `created_at`, `updated_at` | TIMESTAMPTZ | Trigger-gemanagt |

**Bewusst nicht gespeichert:** das volle Roh-JSON (Storage!), Cover-Pfade,
Beschreibungen, volatile Felder wie Partner-Download-URLs.

### sync_releases

Release-Buchhaltung für idempotente, fortsetzbare Syncs:

```text
id BIGSERIAL PK
collection, release_identifier   UNIQUE(collection, release_identifier)
btih                             BitTorrent info-hash
source_url                       .torrent-Download-URL
data_size_bytes                  komprimierte Payloadgröße aus dem Manifest
status                           discovered|downloading|importing|validating|
                                 completed|failed|blocked_storage
discovered_at/started_at/completed_at
records_seen/inserted/updated/skipped/discarded/failed
download_done_bytes/download_total_bytes   (Live-Torrent-Fortschritt fürs Dashboard)
error_message
```

### schema_migrations

`version INT PK, name, applied_at` – geführt von `common.db.apply_migrations`
über die Dateien `migrations/0001_init.sql`, … (unveränderlich, nur anfügen).

## Indizes (und warum nicht mehr)

| Index | Zweck |
|---|---|
| GIN `search_tsv` | Freitextsuche (Titel/Autor/Verlag) |
| GIN `isbn13`, `isbn10`, `doi` | Exakte Identifier-Lookups ohne FTS |
| GIN `author_tokens` | Strukturierter Autor-Filter (`@>` Token-Array) |
| Btree `work_key` (partial) | Logische Werkgruppierung durch FE.Library |

Bewusst weggelassen (Speicher): Trigram-Indizes, Btree auf `title_norm`,
Composite `(extension, publication_year)` – Filters werden gemeinsam mit FTS/
Lookups ausgeführt; Standalone-Browsing ist kein Anwendungsfall. Jeder weitere
Index muss einen nachweisbaren API-Nachweis haben.

## Merge Strategy (metadata quality merge)

Score = gewichtete Präsenz von Feldern (Titel 10, Autoren 8, ISBN13 6, Verlag/Jahr
je 3, ISBN10/DOI je 3, OCLC/OL je 2, Sprache 2, Format/Größe je 1).

Bei Konflikt desselben MD5:

1. **Arrays** (Autoren, Sprachen, Identifier): Vereinigung, dedupliziert,
   Reihenfolge stabil. Information wird nie verworfen.
2. **Skalare** (Titel, Verlag, Jahr, Format, Größe): Die Seite mit dem höheren
   `quality_score` gewinnt. Bei Gleichgewicht gewinnt der neuere
   `source_timestamp`; bei erneutem Gleichstand bleibt der gespeicherte Wert.
   Nichts wird durch leere Werte genullt.
3. **Tombstones**: Ein `removed`-Record markiert die Zeile gelöscht. Nur ein
   streng neuerer Record ohne Removed-Flag belebt sie wieder.
4. **Provenance**: `source_collection`/`source_record_id` zeigen auf den
   besten je gesehenen Datensatz; `aacid` auf den zuletzt verarbeiteten.

Implementierung: ausschließlich `common.records.merge_records` (getestet);
die Pipeline nutzt dieselbe Funktion → keine zweite SQL-Merge-Wahrheit.

## work_key-Ableitung

Priorität: `isbn13[0]` → `isbn10[0]` → `doi[0]` → OpenLibrary-ID, sonst NULL.
Format: `isbn:978…` / `doi:10…/…` / `ol:OL123M`. Titel+Autor-Ähnlichkeit wird
bewusst **nicht** als Identität verwendet (Fehlmerge-Gefahr); Ähnlichkeit fließt
nur als Such-Ranking ein.

## Search Vector

Trigger `trg_metadata_records_tsv` baut bei jedem INSERT/UPDATE:

```sql
setweight(to_tsvector('simple', title_norm), 'A')
|| setweight(to_tsvector('simple', array_to_string(author_tokens,' ')), 'B')
|| setweight(to_tsvector('simple', publisher), 'C')
```

Konfiguration `'simple'` + Python-seitig normalisierte Spalten ⇒ Suche nach
„zizek“ findet „Žižek“, mehrsprachig ohne Stemming-Überraschungen. Query-Seite:
`websearch_to_tsquery('simple', normalize_text(q))` bzw. Token-Präfixe (`tok:*`)
in `app/search.py`.

## Identifier-Normalisierung

* **ISBN-13/10**: Trenner entfernt, Checksumme geprüft, ISBN-10→13 konvertiert;
  ASINs (B…) werden verworfen. Ungültige Werte werden nicht indexiert.
* **DOI**: Prefixe `https://doi.org/`, `doi:`, `info:doi/` entfernt, Formvalidiert
  (`10.\d{4,9}/…`), Prefix lowercase.
* **Sprache**: Namen/Codes → ISO-639-3 (`german|de|ger|deu → deu`), Region-
  Suffixe (`en-us`) auf Basissprache reduziert.
* **Format**: lowercase, plausible Endung `[a-z0-9]{1,10}`, sonst verworfen.

## Sync State & Transaktionale Sicherheit

Ein Release durchläuft `discovered → downloading → importing → validating →
completed`; jeder Fehler endet in `failed` (+ `error_message`), Storage-Guard
Blockaden in `blocked_storage`. Counter werden pro Batch persistiert. Ein
PostgreSQL Advisory Lock (`pg_try_advisory_lock(0x41414D45)`) garantiert, dass
nie zwei Importprozesse parallel laufen – auch nicht nach Container-Restarts.
