# AA Metadata Worker – Architektur

## Systemübersicht

```text
metadata source (Anna's Archive AAC)
     ↓  /dyn/torrents.json + BitTorrent (.torrent)
downloader (sync worker, eingebetteter libtorrent-Client)
     ↓  .jsonl.seekable.zst im sync_work Volume
stream parser (zstandard stream reader → JSONL-Zeilen)
     ↓  eine Zeile = {"aacid": "...", "metadata": {...}}
normalizer (source adapter → NormalizedRecord)
     ↓  ISBN/DOI/Sprache/Format-Normalisierung, Quality-Score
batch buffer (SYNC_BATCH_SIZE Zeilen)
     ↓
PostgreSQL
   - SELECT bestehender Rows für den Batch
   - Merge in der Anwendung (eine Implementierung: common.records.merge_records)
   - INSERT … ON CONFLICT (md5) DO UPDATE
     ↓
REST API (/api/v1, FastAPI + psycopg Pool)
     ↓ HTTP
FE.Library (und andere lokale Clients)
```

## Komponenten

### sync/discovery.py — Release Discovery

* Lädt das offizielle Manifest `{AA_MIRROR_BASE_URL}/dyn/torrents.json`
  (Retry mit Backoff).
* Filtert `is_metadata=true`, `obsolete=false` und matcht
  `annas_archive_meta__aacid__<collection>__<from>--<to>.jsonl.seekable.zst`.
* Wählt pro Collection das Release mit dem neuesten `to`-Timestamp. Die Releases
  sind kumulative Bereiche; frühere Records sind byte-identisch enthalten.

### sync/torrent_client.py — Download

* Eingebetteter libtorrent-Session-Client; lädt die `.torrent`-Datei per HTTPS
  (`/dyn/small_file/torrents/managed_by_aa/…`) und danach die Payload.
* Fortschritts-Logging alle 30 s, Stall-Erkennung (15 min ohne Fortschritt →
  Abbruch als `failed`).
* Payload landet ausschließlich im `sync_work`-Volume (temporär).

### sync/importer.py — Streaming-Pipeline

* `iter_jsonl`: `zstandard.stream_reader` über die Datei → zeilenweises Lesen.
  RAM-Verbrauch konstant unabhängig von der Dateigröße (~Batchgröße).
* Pro Zeile: Adapter-Parsing; Fehler pro Record werden gezählt (max. 20 Samples
  im Log), ein einzelner defekter Record stoppt nie den Import.
* Fehlerquote > `SYNC_ERROR_ABORT_RATE` (Default 2 %) nach ≥10k Records bricht
  den Release-Import ab → Status `failed`.
* Batches: `SELECT` bestehender MD5s → Merge in Python →
  `INSERT … ON CONFLICT` in einer Transaktion → Counter-Update in
  `sync_releases`. Daraus folgt Idempotenz und Resume auf Release-Level.
* SIGTERM: Handler setzt Flag; zwischen zwei Batches wird sauber beendet und
  der Release zurück auf `discovered` gesetzt (wiederaufnahmebereit).

### sync/sources/* — Source Adapter (Phase-3-Architektur)

Ein Adapter pro Collection, keine `if collection == …`-Kaskaden:

| Adapter | Collection | Besonderheiten |
|---|---|---|
| `zlib3.Zlib3Adapter` | `zlib3_records` | `md5_reported`, Sprache als englischer Name, ASINs werden gefiltert, `annabookinfo.response.ipfs_cid` wird extrahiert, `removed/removalReason` → Tombstone |
| `ia2.Ia2Adapter` | `ia2_records` | Ein Record pro Datei aus `aa_shorter_files` (md5 je Datei), Item-Metadaten vererbt, OCLC/OpenLibrary-IDs |
| `uploads.UploadsAdapter` | `upload_records` | Best-effort aus `exiftool_output`/`pikepdf_docinfo`/Dateipfad; `deleted_as_duplicate` → Tombstone |

Neue Collections = neuer Adapter + Registrierung in `sync/sources/__init__.py`.

### common/* — Gemeinsame Kernlogik

* `normalize.py`: Unicode/Diakritika-Normalisierung (NFKD, casefold),
  MD5→16 Bytes, ISBN-10/13 inkl. Checksumme & Konvertierung, DOI,
  ISO-639-3-Sprachen, Format-/Jahr-Normalisierung, `work_key`-Ableitung.
  **Indexierung und Query-Normalisierung nutzen exakt dieselben Funktionen**;
  deshalb braucht es kein PostgreSQL-`unaccent`.
* `records.py`: `NormalizedRecord`, deterministisches `quality_score`,
  Feld-für-Feld-Merge-Strategie (siehe docs/data-model.md).
* `db.py`: psycopg-Pool + versionierte SQL-Migrationen
  (`migrations/NNN_*.sql`, Tabelle `schema_migrations`).

### app/ — REST API

* FastAPI mit Lifespan: Startup wartet mit Backoff auf die DB und fährt
  Migrationen automatisch hoch (Retry statt hartem Crash bei Startreihenfolge).
* Connection-Pool mit `statement_timeout` (Default 5 s) gegen runaway Queries.
* Keyset-Pagination über `(rank DESC, md5 ASC)`; Cursor ist Base64-JSON.
* Bearer-Auth-Middleware (nur aktiv wenn `METADATA_API_KEY` gesetzt); Health-
  Endpoints und OpenAPI sind ausgenommen.

## Netzwerk

```text
Host
 ├─ 127.0.0.1:${API_PORT:-8010} ──> api  (Loopback-Publish für Admin/Curl)
 ├─ docker_bridge (extern)  ─────> api   (FE.Library → http://aa_metadata_api:8010)
 └─ internal (compose default) ──> postgres (NIEMALS nach außen)
```

## Backup / Restore

* Rebuild-Pfad: Bootstrap erneut ausführen (Daten sind öffentlich reproduzierbar).
* Separat sichern: `docker-compose.yaml` (enthält Konfiguration/Passwort),
  optional Dump von `sync_releases`.
* Optionales externes Backup:
  `docker compose exec postgres pg_dump -Fc -U aa_metadata aa_metadata > /mnt/backup/aa_metadata_$(date +%F).dump`
  – Ziel muss **außerhalb** des Budget-Volumes liegen.
* Restore: `pg_restore -U aa_metadata -d aa_metadata --clean --if-exists dumpfile`

## Storage-Benchmark

`python -m sync.cli storage-report` liefert:

```text
records:                  28.734.521
database size:                 XX.XX GiB
bytes/record:                    ~XXXX
disk free (work dir):          XXX.XX GiB
warn/stop thresholds:      300 / 400 GiB
projection @30M records @current bytes/record: XX.X GiB
top relations (heap + indexes): …
```

Vorgehen für die Projektion: repräsentativen Teilimport fahren (≥1 Mio.
Records), `storage-report` ausführen, bytes/record × erwartete Gesamtzahl.
Liegt die Projektion über dem konfigurierten Budget, blockiert der Guard ohnehin
vor weiteren Downloads – zusätzlich bewusst manuell prüfen und ggf. Collections
reduzieren (`AA_COLLECTIONS`).
