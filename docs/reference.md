# AA Metadata Worker – Komplette Referenz

Dieses Dokument beschreibt den gesamten Codeprojekt "aa-metadata-worker" von
Grund auf: Architektur, Datenfluss, Datenbank, API, Sync-Pipeline, Konfiguration,
Deployment und Wartung. Es ist für einen Agenten oder Entwickler geschrieben,
der den Code verstehen und modifizieren soll.

---

## 1. Projektziel

Eigenständiger Metadaten- und Suchdienst: importiert Anna's-Archive-Container
(AAC) lokal, hält sie inkrementell aktuell (PostgreSQL + FTS) und stellt sie
FE.Library über eine stabile REST-API (`/api/v1`) bereit.

**Nicht enthalten:** Buchdateien (kein Download, kein Hosting, kein Streaming).
Der Dienst speichert ausschließlich Metadaten.

---

## 2. Repository-Struktur

```
aa-metadata-worker/
├── AGENTS.md                  # Non-Negotiables, Conventions, Architecture Map
├── Dockerfile                 # Multi-Stage Build, USER metadata (UID 999)
├── docker-compose.yaml        # Produktionsstack (postgres + api)
├── docker-compose.local.yaml  # Lokaler Build (Rollback-Option)
├── pyproject.toml             # pytest + ruff config
├── requirements.txt           # Python-Dependencies
├── Makefile                   # setup, check, build
│
├── app/                       # FastAPI REST API
│   ├── main.py                # App-Factory, Lifespan (DB + Sync-Worker-Thread)
│   ├── deps.py                # FastAPI Dependencies (DB-Pool, Settings)
│   ├── schemas.py             # Pydantic Response Models
│   ├── search.py              # SQL-Building, Keyset-Pagination, Row→Model
│   └── routes/
│       ├── health.py          # GET /api/v1/health/live, /health/ready
│       ├── status.py          # GET /api/v1/status
│       ├── search.py          # GET /api/v1/search
│       ├── records.py         # GET /api/v1/records/{md5}, /records/{md5}/sources
│       ├── dashboard.py       # GET /, /dashboard, /api/v1/sync/status
│       └── control.py         # GET /api/v1/sync/control, POST /api/v1/sync/commands
│
├── common/                    # Gemeinsame Kernlogik (API + Sync)
│   ├── config.py              # Settings-Dataclass, load_settings() aus Env-Variablen
│   ├── db.py                  # psycopg Pool, Migrationen, approx_count()
│   ├── normalize.py           # ISBN/DOI/Sprache/Text/Format-Normalisierung
│   ├── records.py             # NormalizedRecord, quality_score(), merge_records()
│   └── languages.py           # ISO-639-3 Mapping (500+ Sprachen)
│
├── sync/                      # Sync-Pipeline (im API-Container als Daemon-Thread)
│   ├── cli.py                 # CLI: status, check, run, bootstrap, retry, worker
│   ├── worker.py              # Scheduler: Sleep bis SYNC_SCHEDULE, CommandPoller
│   ├── run.py                 # Orchestrierung: SyncRunSummary, run_sync()
│   ├── discovery.py           # Manifest-Fetch, ReleaseInfo, latest_releases()
│   ├── torrent_client.py      # libtorrent Wrapper, Delta-Downloads (Hardlink)
│   ├── importer.py            # Streaming-Import: iter_jsonl → Batch → Upsert
│   ├── state.py               # DB-State: sync_releases, Advisory Lock, Commands
│   ├── storage_guard.py       # Storage-Budget: evaluate_storage()
│   └── sources/               # Source Adapter (einer pro Collection)
│       ├── __init__.py        # Adapter-Registry
│       ├── base.py            # SourceAdapter ABC, aacid_timestamp()
│       ├── zlib3.py           # Z-Library Adapter
│       ├── ia2.py             # Internet Archive Adapter
│       └── uploads.py         # Anna's Archive Uploads Adapter
│
├── migrations/                # Versionierte SQL-Migrationen
│   ├── 0001_init.sql          # metadata_records, sync_releases, Trigger, Indizes
│   ├── 0002_release_discarded.sql
│   ├── 0003_download_progress.sql
│   ├── 0004_sync_control.sql  # sync_commands, sync_control_state
│   └── 0005_series_edition.sql  # series_name, series_position, edition
│
├── tests/                     # unit | integration | api | fixtures
│
└── docs/                      # Dokumentation
    ├── reference.md           # ← dieses Dokument
    ├── architecture.md        # Architektur-Übersicht
    ├── data-model.md          # Datenmodell, Merge-Strategie
    ├── sync.md                # Sync-Pipeline Details
    └── fe-library-integration.md  # FE.Library Client-Beispiel
```

---

## 3. Container-Architektur

### 3.1 Produktions-Stack (`docker-compose.yaml`)

Zwei Container, verbunden über das `internal`-Bridge-Netzwerk:

```
┌─────────────────────────────────────────────────────────┐
│  docker_bridge (extern)                                  │
│    aa_metadata_api:8010  ← FE.Library, Dashboard, curl  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────────┐
│  internal (Bridge)                                       │
│    aa_metadata_api ←→ aa_metadata_postgres               │
│    (API + Sync-Worker-Thread)     (PostgreSQL 17.6)      │
│                                                          │
│  Volumes:                                                │
│    ./data/sync_work:/work/sync  (read-write)             │
│    ./data/postgres:/var/lib/postgresql/data              │
└──────────────────────────────────────────────────────────┘
```

### 3.2 Image-Build (`Dockerfile`)

Multi-Stage Build:

1. **Build-Stage**: `python:3.11-slim-bookworm` – baut Wheels aus `requirements.txt`
2. **Runtime-Stage**: Installiert Wheels, erstellt User `metadata` (GID/UID 999),
   kopiert `app/`, `common/`, `sync/`, `migrations/` nach `/app`.
3. **CMD**: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8010`
4. **Security**: `USER metadata`, `read_only: true`, `cap_drop: ALL`, `no-new-privileges`.

### 3.3 API + Sync im selben Container

Der Sync-Worker läuft als **Daemon-Thread** innerhalb des API-Prozesses:

```python
# app/main.py – lifespan
_start_sync_worker()   # → threading.Thread(target=run_worker_forever, daemon=True)
```

Vorteil: Kein separater Container nötig, geteilte Konfiguration, keine
Inter-Container-Kommunikation für den Sync. Der Worker teilt sich den
PostgreSQL-Pool mit der API (eigene Connection für den Lock).

---

## 4. Datenbank-Schema

### 4.1 `metadata_records` – Haupttabelle

Primäre Entität: ein Metadata-Record = ein konkreter Source-/File-Record,
identifiziert durch MD5 (16 Bytes, `BYTEA`). MD5 ≠ logisches Buch.

| Spalte | Typ | Beschreibung |
|---|---|---|
| `md5` | `BYTEA(16)` PK | Roher MD5 (API liefert Hex) |
| `title` | `TEXT` | Original-Titel |
| `title_norm` | `TEXT` | Normalisiert (NFKD, casefold, ohne Diakritika) |
| `authors` | `TEXT[]` | Autorenliste (Originalschreibweise) |
| `author_tokens` | `TEXT[]` | Normalisierte Einzelwörter (GIN-Containment) |
| `publisher` | `TEXT` | Verlag |
| `publication_year` | `SMALLINT` | 1000–2100 |
| `languages` | `TEXT[]` | ISO-639-3 (`deu`, `eng`) |
| `extension` | `TEXT` | Format (lowercase, `epub`, `pdf`) |
| `filesize` | `BIGINT` | Bytes |
| `isbn10`, `isbn13`, `doi`, `oclc`, `openlibrary_ids` | `TEXT[]` | Normalisierte Identifiers (Arrays) |
| `work_key` | `TEXT NULL` | Logische Werk-ID (`isbn:978…`, `doi:…`, `ol:…`) |
| `series_name` | `TEXT` | Reihenname (z.B. `"Wicked Games"`) |
| `series_position` | `SMALLINT` | Bandnummer in der Reihe |
| `edition` | `TEXT` | Auflage (z.B. `"2nd Edition"`) |
| `source_collection` | `TEXT` | `zlib3_records` / `ia2_records` / `upload_records` / `goodreads_records` / `gbooks_records` / `libby_records` |
| `source_record_id` | `TEXT` | Quellen-ID (z.B. `zlibrary_id`) |
| `aacid` | `TEXT` | AACID des letzten Import-Records |
| `source_timestamp` | `TIMESTAMPTZ` | Scrape-Timestamp aus dem AACID |
| `quality_score` | `SMALLINT` | Deterministischer Vollständigkeits-Score |
| `deleted` | `BOOLEAN` | Tombstone-Flag |
| `removed_reason` | `TEXT` | `removed` (zlib) / `deleted_as_duplicate` (uploads) |
| `ipfs_cid` | `TEXT` | Stabile Inhaltsreferenz |
| `search_tsv` | `TSVECTOR` | FTS-Feld (Trigger gepflegt) |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | Trigger-gemanagt |

### 4.2 `sync_releases` – Sync-Buchhaltung

Tracking aller importierten Releases (idempotent, fortsetzbar):

| Spalte | Typ | Beschreibung |
|---|---|---|
| `id` | `BIGSERIAL` PK | |
| `collection` + `release_identifier` | `TEXT` UNIQUE | Eindeutige Release-Kennung |
| `btih` | `TEXT` | BitTorrent info-hash |
| `source_url` | `TEXT` | .torrent-Download-URL |
| `data_size_bytes` | `BIGINT` | Komprimierte Payloadgröße |
| `status` | `TEXT` | `discovered` → `downloading` → `importing` → `validating` → `completed` / `failed` / `blocked_storage` |
| `download_done_bytes` | `BIGINT` | Live-Torrent-Fortschritt |
| `download_total_bytes` | `BIGINT` | |
| `records_seen/inserted/updated/skipped/discarded/failed` | `BIGINT` | Import-Counter |
| `error_message` | `TEXT` | Fehler-Details |
| `discovered_at/started_at/completed_at` | `TIMESTAMPTZ` | Zeitstempel |

### 4.3 `sync_commands` + `sync_control_state`

Dashboard-gesteuerte Befehlsqueue für den Worker:

- `sync_commands`: `id`, `command` (`run_now`/`pause`/`resume`), `created_at`, `picked_at`
- `sync_control_state`: Key/Value (`paused` = `true`/`false`)

### 4.4 `schema_migrations`

`version INT PK`, `name TEXT`, `applied_at TIMESTAMPTZ` – geführt von `common.db.apply_migrations`.

### 4.5 Indizes

| Index | Typ | Zweck |
|---|---|---|
| `idx_metadata_search_tsv` | GIN | Freitextsuche (Titel A, Autor B, Verlag C) |
| `idx_metadata_isbn13` | GIN | Exakte ISBN-13-Lookups |
| `idx_metadata_isbn10` | GIN | Exakte ISBN-10-Lookups |
| `idx_metadata_doi` | GIN | Exakte DOI-Lookups |
| `idx_metadata_author_tokens` | GIN | Strukturierter Autor-Filter (`@>` Token-Array) |
| `idx_metadata_work_key` | Btree (partial) | Logische Werkgruppierung |

### 4.6 Trigger

- `trg_metadata_records_tsv`: Baut `search_tsv` aus `title_norm` (A), `author_tokens` (B), `publisher` (C) mit `to_tsvector('simple', …)`.
- `trg_metadata_records_updated_at`: Setzt `updated_at` bei jedem UPDATE.

### 4.7 Migrationen

Dateien: `migrations/NNN_name.sql` (4 Ziffern, strikt steigend). Bereits
angewendete Migrationen werden nie editiert. Neue Schemaänderungen nur über
neue Dateien. Tracking in `schema_migrations`.

---

## 5. Datenfluss (Import-Pipeline)

```
Anna's Archive torrents.json
  │
  ▼  fetch_manifest() – GET {AA_MIRROR_BASE_URL}/dyn/torrents.json
  │
  ▼  latest_releases() – pro Collection neuestes Release wählen
  │
  ▼  ensure_release() – sync_releases-Row anlegen/aktualisieren
  │
  ▼  Storage-Guard prüfen (evaluate_storage)
  │
  ▼  TorrentClient.download() – .torrent laden, Payload per BitTorrent
  │  (Delta-Download: Hardlink von Previous-Payload → nur geänderte Pieces)
  │  Payload landet in /work/sync/{release_identifier}
  │
  ▼  Storage-Guard vor Import
  │
  ▼  import_release() – Streaming-Import
  │  │
  │  ▼  iter_jsonl() – zstandard.stream_reader → zeilenweise JSONL
  │  │
  │  ▼  adapter.parse(raw) – Quellenspezifisches Parsing
  │  │  (Zlib3Adapter / Ia2Adapter / UploadsAdapter)
  │  │  → NormalizedRecord (oder 0..n Records)
  │  │
  │  ▼  Book-Filter (nicht-Buch-Material wird verworfen)
  │  │
  │  ▼  Batch-Buffer (SYNC_BATCH_SIZE Zeilen)
  │  │
  │  ▼  process_batch():
  │     1. Duplikate im Batch falten (merge_records)
  │     2. SELECT bestehende MD5s aus DB
  │     3. Merge in Python (merge_records)
  │     4. INSERT … ON CONFLICT DO UPDATE (atomar)
  │     5. Counter in sync_releases persistieren
  │
  ▼  validate_import() – Prüfungen (Records > 0, Fehlerquote ok)
  │
  ▼  Status → completed, Payload löschen oder als Seed-Basis behalten
```

### 5.1 Source Adapter

Ein Adapter pro Collection, registriert in `sync/sources/__init__.py`:

| Adapter | Collection | Besonderheiten |
|---|---|---|
| `Zlib3Adapter` | `zlib3_records` | `md5_reported`, Sprache als englischer Name, ASINs gefiltert, `ipfs_cid`, Tombstone via `removed/removalReason` |
| `Ia2Adapter` | `ia2_records` | Ein Record pro Datei aus `aa_shorter_files`, Item-Metadaten vererbt, OCLC/OpenLibrary-IDs, `mediatype != "texts"` → Discard |
| `UploadsAdapter` | `upload_records` | Best-effort aus `exiftool_output`/`pikepdf_docinfo`, Subcollection-Blocklist, `deleted_as_duplicate` → Tombstone |
| `GoodreadsAdapter` | `goodreads_records` | Goodreads-XML in `metadata.record`; synthetischer MD5 (`synthetic_md5(collection, id)`), ISBN/Sprache/Jahr aus XML |
| `GbooksAdapter` | `gbooks_records` | Google-Books-JSON; `industryIdentifiers` → ISBNs; `printType != "BOOK"` → Discard (`AA_GBOOKS_REQUIRE_BOOKS`) |
| `LibbyAdapter` | `libby_records` | OverDrive-JSON; Creator-Rolle „Author"; Publisher→Imprint-Fallback; Medientyp nicht in `AA_LIBBY_ALLOWED_TYPES` → Discard |

Die drei Anreicherungs-Collections enthalten keine Datei-MD5s. Der
deterministische Schlüssel `synthetic_md5(collection, record_id)` (SHA-256,
16 Bytes) sorgt dafür, dass Re-Imports auf dieselbe Zeile mergen und echte
Datei-MD5s praktisch nie kollidieren.

Adapter-Vertrag (`SourceAdapter` ABC):
```python
class SourceAdapter(ABC):
    collection: str
    def parse(self, raw: dict) -> list[NormalizedRecord]: ...
```

Neue Collection = neuer Adapter + Registrierung in `__init__.py`.

### 5.2 Merge-Strategie (`common.records.merge_records`)

Score = gewichtete Präsenz von Feldern (Titel 10, Autoren 8, ISBN13 6, Verlag/Jahr
je 3, ISBN10/DOI je 3, OCLC/OL je 2, Sprache 2, Format/Größe je 1).

Bei Konflikt desselben MD5:
1. **Arrays** (Autoren, Sprachen, Identifier): Union, dedupliziert, Reihenfolge stabil.
2. **Skalare** (Titel, Verlag, Jahr, Format, Größe): Höherer Score gewinnt. Bei Gleichstand: neuerer Timestamp. Nichts wird durch leere Werte genullt.
3. **Tombstones**: `removed` bleibt, bis ein streng neuerer Record ohne Removed-Flag belebt.
4. **Provenance**: `source_collection`/`source_record_id` zeigen auf den besten je gesehenen Datensatz; `aacid` auf den zuletzt verarbeiteten.

**Non-Negotiable**: Merge-Strategie nur in `common.records.merge_records` ändern (mit Tests!).

---

## 6. Normalisierung (`common/normalize.py`)

**Non-Negotiable**: Indexierung und Query-Normalisierung nutzen exakt dieselben Funktionen.

| Funktion | Input → Output | Beispiel |
|---|---|---|
| `normalize_text()` | NFKD + casefold + Diakritika-Stripping | `"Žižek"` → `"zizek"` |
| `normalize_md5()` | Hex-String → 16 Bytes oder None | |
| `normalize_isbn13()` | Stripping, Checksumme, 10→13 | `"978-3-16-148410-0"` → `"9783161484100"` |
| `normalize_isbn10()` | Stripping, Checksumme | ASINs (`B…`) werden verworfen |
| `normalize_doi()` | Prefix-Stripping, Formvalidierung | `"https://doi.org/10.1234/foo"` → `"10.1234/foo"` |
| `normalize_language()` | Name/Code → ISO-639-3 | `"german"` → `"deu"`, `"de"` → `"deu"` |
| `normalize_extension()` | Lowercase, plausibel | `"EPUB"` → `"epub"` |
| `normalize_year()` | Extraktion aus chaotischem Input | `"©2020"` → `2020` |
| `split_authors()` | Aufteilung nach Semikolon/Pipe/`&`/`and` | `"Doe, Jane; Smith, John"` → `["Doe, Jane", "Smith, John"]` |
| `derive_work_key()` | Priorität: isbn13 → isbn10 → doi → ol | `"9783161484100"` → `"isbn:9783161484100"` |

---

## 7. REST API (`/api/v1`)

FastAPI-App, gestartet via Uvicorn. Keyset-Pagination über `(rank DESC, md5 ASC)`.

### 7.1 Endpunkte

| Methode | Pfad | Beschreibung | Auth-Exempt |
|---|---|---|---|
| `GET` | `/api/v1/health/live` | Liveness-Check | Ja |
| `GET` | `/api/v1/health/ready` | Readiness (Migrations angewendet?) | Ja |
| `GET` | `/api/v1/status` | System-Status (Records, DB-Größe, Sync) | Nein |
| `GET` | `/api/v1/search` | Freitextsuche + Filter | Nein |
| `GET` | `/api/v1/records/{md5}` | Record-Detail | Nein |
| `GET` | `/api/v1/records/{md5}/sources` | Quellen-Referenzen | Nein |
| `GET` | `/api/v1/editions/{work_key}` | Alle Versionen eines Werks | Nein |
| `GET` | `/api/v1/sync/status` | Sync-Status (pro Collection) | Ja |
| `GET` | `/api/v1/sync/control` | Worker-Steuerung (paused, schedule) | Ja |
| `POST` | `/api/v1/sync/commands` | Befehl an Worker (run_now/pause/resume) | Nein |
| `GET` | `/dashboard` | HTML-Dashboard | Ja |
| `GET` | `/` | Redirect → `/dashboard` | Ja |
| `GET` | `/openapi.json`, `/docs`, `/redoc` | OpenAPI | Ja |

### 7.2 Search-Endpoint (`GET /api/v1/search`)

**Query-Parameter:**

| Parameter | Typ | Beschreibung |
|---|---|---|
| `q` | string (max 200) | Freitextsuche (FTS über `search_tsv`) |
| `title` | string (max 200) | Titel-Filter (Token-Prefix via FTS) |
| `author` | string (max 200) | Autor-Filter (GIN `@>` auf `author_tokens`) |
| `isbn` | string (max 20) | ISBN-Lookup (10 oder 13, Bindestriche erlaubt) |
| `doi` | string (max 100) | DOI-Lookup |
| `language` | string (max 30) | ISO-639-3 oder 2-Letter (normalisiert) |
| `series` | string (max 200) | Reihenname (Token-Prefix via FTS) |
| `series_position` | int (1–9999) | Bandnummer in der Reihe |
| `extension` | string (max 10) | Format (`epub`, `pdf`, …) |
| `year_from` | int (1000–2100) | Untergrenze |
| `year_to` | int (1000–2100) | Obergrenze |
| `limit` | int (1–100, default 20) | Seitengröße |
| `cursor` | string | Keyset-Pagination-Cursor |

Mindestens ein Parameter ist erforderlich. `year_from <= year_to` enforced.

Beispiele:
- Suche nach einer Reihe: `GET /api/v1/search?series=wicked+games`
- Alle Bände der Reihe: `GET /api/v1/search?series=wicked+games&series_position=1`
- Reihe + Sprache: `GET /api/v1/search?series=hermiony&language=deu&extension=epub`

**Antwort:**
```json
{
  "totalLowerBound": 42,
  "limit": 20,
  "nextCursor": "eyJyIjoiMC4xMjM0IiwibSI6IjAxMmM…",
  "results": [
    {
      "md5": "0123456789abcdef0123456789abcdef",
      "title": "Example Book",
      "authors": ["Jane Doe"],
      "publisher": "Pub House",
      "publicationYear": 2020,
      "languages": ["deu"],
      "format": "epub",
      "filesize": 1234567,
      "identifiers": {
        "isbn10": [],
        "isbn13": ["9783161484100"],
        "doi": [],
        "oclc": [],
        "openlibrary": []
      },
      "workKey": "isbn:9783161484100",
      "seriesName": "Wicked Games",
      "seriesPosition": 1,
      "edition": null,
      "source": {
        "collection": "zlib3_records",
        "record_id": "22433983",
        "aacid": "aacid__…"
      },
      "editionCount": 2
    }
  ]
}
```

### 7.3 Record-Detail (`GET /api/v1/records/{md5}`)

32-Hex-Zeichen MD5. Gibt `RecordResponse` zurück. `404` bei Nichtfinden.

### 7.4 Record-Sources (`GET /api/v1/records/{md5}/sources`)

Gibt Referenzinformationen zurück (AA-Page-URL, IPFS-CID). Keine
Download-URLs. `410` wenn Record gelöscht.

```json
{
  "md5": "…",
  "aaPageUrl": "https://annas-archive.org/md5/…",
  "ipfsCID": "…",
  "source": { "collection": "…", "record_id": "…", "aacid": "…" },
  "note": "Reference information only…"
}
```

### 7.5 Editions (`GET /api/v1/editions/{work_key}`)

Alle Versionen eines Werks, sortiert nach Quality Score (beste Version zuerst).
`work_key` Format: `isbn:978…`, `doi:10…/…`, `ol:OL123M`.

```json
{
  "workKey": "isbn:9783161484100",
  "totalEditions": 3,
  "editions": [
    {
      "md5": "…",
      "title": "Enchantra",
      "format": "epub",
      "filesize": 1234567,
      "seriesName": "Wicked Games",
      "seriesPosition": 1,
      "edition": null,
      "source": { "collection": "zlib3_records", "record_id": "…", "aacid": "…" }
    },
    {
      "md5": "…",
      "title": "Enchantra",
      "format": "pdf",
      "filesize": 2345678,
      "seriesName": "Wicked Games",
      "seriesPosition": 1,
      "edition": "Reissue",
      "source": { "collection": "ia2_records", "record_id": "…", "aacid": "…" }
    }
  ]
}
```

`404` wenn kein Record mit diesem `work_key` existiert.

### 7.5 Status (`GET /api/v1/status`)

```json
{
  "ready": true,
  "records": 24889217,
  "lastSuccessfulSync": "2026-08-21T04:17:31+00:00",
  "collections": ["zlib3_records", "ia2_records", "goodreads_records", "gbooks_records", "libby_records", "upload_records"],
  "databaseSizeBytes": 8589934592,
  "diskFreeBytes": 107374182400,
  "schemaVersion": 4,
  "sync": {
    "status": "running",
    "activeRelease": "annas_archive_meta__aacid__zlib3_records__…"
  }
}
```

`records` nutzt `approx_count()`: versucht `SELECT COUNT(*)`, bei
`QueryCanceled` (SQLSTATE 57014) Fallback auf `pg_class.reltuples`.

### 7.6 Sync-Status (`GET /api/v1/sync/status`)

Dashboard-Endpoint. Liefert pro Collection den neuesten Release + aktiven
Sync + aggregierte Counter. Auth-exempt.

Top-Level `appVersion` enthält den beim Docker-Build eingebrannten Git-Commit
(`APP_VERSION` build-arg, lokal `"dev"`). Das Dashboard zeigt ihn neben der
Überschrift und gleicht ihn mit GitHub ab („✓ aktuell" / „⟳ Update verfügbar").

### 7.7 Sync-Control (`GET /api/v1/sync/control`)

```json
{
  "paused": false,
  "enabled": true,
  "schedule": "03:15",
  "tz": "Europe/Berlin",
  "nextScheduledRun": "2026-08-26T03:15:00+02:00",
  "lastCommand": { "command": "run_now", "createdAt": "…", "picked": true }
}
```

### 7.8 Sync-Commands (`POST /api/v1/sync/commands`)

```json
{ "action": "run_now" | "pause" | "resume", "note": "optional" }
```

Response: `{ "queued": true, "id": 123, "action": "run_now" }`

Worker pollt `sync_commands` alle 10 Sekunden.

### 7.9 Bearer-Auth

Nur aktiv wenn `METADATA_API_KEY` gesetzt. Vergleich via `hmac.compare_digest`.
Auth-exempt: Health, sync/status, sync/control, Dashboard, OpenAPI.

### 7.10 Keyset-Pagination

Cursor = Base64-encoded JSON `{"r": <rank>, "m": "<md5_hex>"}`. Kein OFFSET.
ORDER BY `rank DESC, md5 ASC`. `nextCursor = null` → Ende.

---

## 8. Sync-Worker

### 8.1 Scheduler (`sync/worker.py`)

- Schläft bis `SYNC_SCHEDULE` (HH:MM, TZ).
- Pollt `sync_commands` alle 10 Sekunden.
- Befehle: `run_now` (sofort), `pause` (flag + in-flight interrupt), `resume`.
- Signal-Handler nur in Main-Thread (Daemon-Thread im API-Container → übersprungen).
- Graceful Shutdown: `request_stop()` setzt Flag, Importer beendet zwischen Batches.

### 8.2 Orchestrierung (`sync/run.py`)

`run_sync()`:
1. `fetch_manifest()` → `latest_releases()` pro Collection.
2. `ensure_release()` → sync_releases-Row.
3. Skip wenn `completed` (außer `--force`).
4. Storage-Guard vor Download.
5. `TorrentClient.download()` mit Delta-Download (Hardlink Previous-Payload).
6. Storage-Guard vor Import.
7. `import_release()` → `validate_import()` → Status `completed`.
8. Previous-Payload behalten oder löschen.

`SyncRunSummary`: processed, skipped, blocked, failed, duration_s.

### 8.3 Torrent-Download (`sync/torrent_client.py`)

- Eingebetteter libtorrent-Session-Client.
- `.torrent` per HTTPS laden → Payload per BitTorrent.
- **Delta-Downloads**: Previous-Payload wird hardgelinkt; libtorrent
  hash-checkt alle Pieces, lädt nur geänderte (typ. wenige 100 MB statt 24/146 GB).
- Stall-Erkennung: 15 min ohne Fortschritt → Abbruch.
- Progress-Callback für Live-Fortschritt im Dashboard.

### 8.4 Streaming-Import (`sync/importer.py`)

- `iter_jsonl()`: `zstandard.ZstdDecompressor.stream_reader` → zeilenweise.
- RAM-Verbrauch konstant (~Batchgröße), unabhängig von Dateigröße.
- Pro Zeile: `adapter.parse(raw)`, Fehler werden gezählt (max 20 Samples).
- Batch-Commit: `process_batch()` → SELECT existing → merge → INSERT ON CONFLICT.
- Error-Rate-Abort: > `SYNC_ERROR_ABORT_RATE` (2%) nach ≥10k Records → Abbruch.
- SIGTERM: Handler setzt Flag; zwischen zwei Batches sauber beendet.

### 8.5 Release States

```
discovered → downloading → importing → validating → completed
                         ↘ failed (jederzeit)
              ↘ blocked_storage (vor Download/Import)
```

### 8.6 Advisory Lock

`pg_try_advisory_lock(0x41414D45)` – garantiert, dass nie zwei Importprozesse
parallel laufen (auch nicht nach Container-Restarts).

---

## 9. Konfiguration (`common/config.py`)

Konfiguration ausschließlich über **Env-Variablen** (in `docker-compose.yaml`,
bewusst kein `.env`). `Settings` ist ein frozen Dataclass.

| Variable | Default | Beschreibung |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | DB-Host |
| `POSTGRES_PORT` | `5432` | DB-Port |
| `POSTGRES_DB` | `aa_metadata` | Datenbankname |
| `POSTGRES_USER` | `aa_metadata` | DB-User |
| `POSTGRES_PASSWORD` | `""` | DB-Passwort |
| `API_PORT` | `8010` | API-Port |
| `METADATA_API_KEY` | `""` | Optionaler Bearer-Key (leer = aus) |
| `API_STATEMENT_TIMEOUT_MS` | `5000` | Statement-Timeout (NAS: 20000) |
| `AA_COLLECTIONS` | `zlib3_records,ia2_records,goodreads_records,gbooks_records,libby_records,upload_records` | Zu importierende Collections |
| `AA_MIRROR_BASE_URL` | `https://annas-archive.gd` | AAC-Manifest-URL |
| `SYNC_ENABLED` | `true` | Worker aktivieren |
| `SYNC_SCHEDULE` | `03:15` | HH:MM Europe/Berlin |
| `SYNC_BATCH_SIZE` | `20000` | Records pro Batch |
| `SYNC_ERROR_ABORT_RATE` | `0.02` | Abort-Threshold |
| `AA_UPLOAD_BLOCKED_SUBCOLLECTIONS` | `academia_edu,us_gov_tech_reports,wikilib,aaaaarg,magzdb` | Blockierte Upload-Subcollections |
| `AA_UPLOAD_REQUIRE_TITLE_AUTHOR` | `true` | Uploads ohne Titel+Autor verwerfen |
| `AA_IA_REQUIRE_TEXTS` | `true` | Nur IA "texts" Items |
| `SYNC_REUSE_PREV_PAYLOAD` | `true` | Delta-Downloads aktivieren |
| `STORAGE_WARN_GIB` | `300` | Warn-Schwelle |
| `STORAGE_STOP_GIB` | `400` | Harter Stopp |
| `LOG_LEVEL` | `INFO` | |
| `TZ` | `Europe/Berlin` | |

---

## 10. Storage Guard (`sync/storage_guard.py`)

Budget-Modell: `projizierte Gesamtbelegung = pg_database_size() + ausstehende Bytes`.

- ≥ `STORAGE_STOP_GIB` oder zu wenig freier Platte → Operation startet nicht; Release → `blocked_storage`.
- ≥ `STORAGE_WARN_GIB` → Warnung im Log, Import läuft weiter.

Vor jedem Download **und** vor dem Import geprüft. Annahme: `postgres/` und
`sync_work/` liegen unter demselben Dateisystem.

Bericht: `python -m sync.cli storage-report`.

---

## 11. Deployment

### 11.1 Erstes Deployment

```bash
# NAS: sudo-md
docker compose up -d                    # Stack starten (postgres + api)
docker compose run --rm api python -m sync.cli bootstrap  # Erstimport
```

### 11.2 CI/CD (GitHub Actions)

- Trigger: Push to `main` oder Tag.
- Buildx + GHA-Cache, `linux/amd64`.
- Image: `ghcr.io/frederikemmer/aa-metadata-worker:{sha}` + `:latest`.
- Auto-Update via Dockhand (optional).

### 11.3 NAS-Regeln

- Nur das Projekt-Directory und aa_metadata Container berühren.
- `data/postgres/` und `data/sync_work/` gehören User `metadata` (UID 999).
- `rsync`/`scp` defekt → tar-pipe oder paramiko SFTP für Dateiübertragung.
- SSH-Daemon kann vom System gekillt werden → UGOS Web-UI neu aktivieren.

### 11.4 Wartung

```bash
# Status prüfen
docker compose exec api python -m sync.cli status
docker compose exec api python -m sync.cli db-stats

# Storage-Report
docker compose exec api python -m sync.cli storage-report

# Fehlgeschlagenen Release retry
docker compose exec api python -m sync.cli retry <release_id>

# Dashboard
open http://<host>:8010/dashboard

# API-Docs
open http://<host>:8010/docs

# Backup
docker compose exec postgres pg_dump -Fc -U aa_metadata aa_metadata > backup.dump

# Restore
pg_restore -U aa_metadata -d aa_metadata --clean --if-exists backup.dump
```

---

## 12. Tests

Markers: `unit`, `integration` (echtes PG via Docker), `api`, `sync`, `slow`.

```bash
make setup      # venv + deps
make check      # lint + unit + integration + compose config + docker build
```

**Non-Negotiable**: Merge-Strategie und Normalisierung haben eigene Tests.
Indexierung und Query-Normalisierung nutzen exakt dieselben Funktionen.

---

## 13. Wichtige Design-Entscheidungen

1. **Metadata-only**: Kein Buch-File-Handling. Quellen-Referenzen sind die Grenze.
2. **Kein Web-Scraping**: Nur offizielle Torrents/Manifeste.
3. **PostgreSQL als einziger State**: Kein Redis, kein File-basierter State.
4. **Merge in Python, nicht SQL**: `merge_records()` ist die einzige Wahrheit.
5. **Kumulative Releases**: Neuestes Release enthält alle vorherigen Records.
   Delta-Downloads nutzen byte-identische Compressed-Prefixe.
6. **Kein .env**: Konfiguration in `docker-compose.yaml` (bewusst, für Transparenz).
7. **Single-Container**: API + Sync im selben Container (Daemon-Thread).
8. **Keyset-Pagination**: Stabil, performant, keine OFFSETs.
9. **`'simple'` FTS-Config**: Python-seitig normalisierte Spalten → kein PostgreSQL-unaccent.
10. **Tombstones**: Gelöschte Records bleiben als Markierung; werden nie physisch gelöscht.

---

## 14. Fehlerbehandlung (API)

| Status | Bedeutung |
|---|---|
| 400 | Ungültige Parameter (ISBN/MD5/Cursor/Query) |
| 401 | Bearer-Key falsch/fehlend |
| 404 | Record nicht gefunden |
| 410 | Record an Quelle entfernt (Tombstone) |
| 422 | Query-Limit überschritten |
| 5xx/Timeout | Dienst nicht bereit → Retry mit Backoff |

---

## 15. FE.Library Integration

FE.Library nutzt den Metadata-Dienst über HTTP. Base URL:
`http://aa_metadata_api:8010` (Container-zu-Container im `docker_bridge`-Netz).

Beispiel-Client in `docs/fe-library-integration.md` (httpx, typisiert).

Variablen für FE.Library:
```env
METADATA_API_URL=http://aa_metadata_api:8010
METADATA_API_KEY=          # optional
METADATA_API_TIMEOUT=10
```
