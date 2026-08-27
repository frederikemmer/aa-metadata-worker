# AA Metadata Worker

Ein vollständig eigenständiger, Docker-basierter Dienst, der Buch-Metadaten aus
den öffentlich verfügbaren **Anna's Archive Containers (AAC)** lokal importiert,
regelmäßig aktualisiert, platzsparend in PostgreSQL indexiert und über eine
stabile REST-API (`/api/v1`) bereitstellt.

> **Scope:** Metadaten- und Suchdienst. Dieser Dienst hostet, streamed oder
> proxied **keine** Buchdateien. Er liefert ausschließlich Metadaten plus
> stabile Quellen-Referenzen (AA-Seiten-URL, IPFS-CID), damit Clients wie
> FE.Library die eigentliche Beschaffung selbst übernehmen können.

## 1. Zweck

* Lokaler Suchindex für Millionen von Buch-Metadatensätzen (Z-Library,
  Internet Archive, AA Uploads) – Anna's Archive wird **nicht** bei Suchen
  kontaktiert.
* Inkrementeller, idempotenter Sync aus den offiziellen Metadata-Torrents.
* Stabile REST-API für FE.Library und andere lokale Tools.

## 2. Architektur

```text
Anna's Archive (AAC Metadata Torrents)
        │  torrents.json + BitTorrent
        ▼
┌──────────────────┐     ┌────────────────────┐
│  Sync Worker     │ --> │ Stream-Parser      │
│  (Discovery,     │     │ .jsonl.seekable.zst│
│   Torrent,       │     └─────────┬──────────┘
│   Storage Guard) │               ▼
└────────┬─────────┘     ┌────────────────────┐
         │               │ Normalizer /       │
         │               │ Source Adapter     │
         │               └─────────┬──────────┘
         ▼                         ▼
┌─────────────────────────────────────────┐
│ PostgreSQL (metadata_records,           │
│             sync_releases, FTS-Index)   │
└────────────────────┬────────────────────┘
                     ▼
            ┌──────────────────┐      ┌────────────┐
            │ REST API /api/v1 │ ---> │ FE.Library │
            └──────────────────┘      └────────────┘
```

Details: [docs/architecture.md](docs/architecture.md)

## 3. Voraussetzungen

* Docker + Docker Compose v2
* Für Entwicklung: Python 3.11+ (`make setup`)
* Freier Speicher: siehe Abschnitt Storage (Default-Konfiguration zielt auf
  NAS-Betrieb mit >500 GB)

## 4. Setup – alles in der docker-compose.yaml

Es gibt **keine .env**: Alle Einstellungen stehen direkt und kommentiert in
`docker-compose.yaml`. Vor dem ersten Start dort anpassen:

1. `POSTGRES_PASSWORD` (dreimal: postgres, api, sync – muss identisch sein)
2. Volume-Pfade `./data/postgres` / `./data/sync_work` (NAS-Mount eintragen,
   beide müssen auf demselben Dateisystem liegen)

```bash
docker network create docker_bridge   # falls noch nicht vorhanden (bestehende Infra)
docker compose up -d
```

## 5. Konfiguration (Auszug aus der docker-compose.yaml)

| Variable | Default | Bedeutung |
|---|---|---|
| `POSTGRES_*` | – | DB-Zugang (nur intern; Passwort in allen 3 Services gleich) |
| `METADATA_API_KEY` | leer | Optionaler Bearer-Schutz für Daten-Endpoints (Dashboard bleibt offen) |
| `AA_COLLECTIONS` | `zlib3_records,upload_records` | Aktive Buchquellen im Suchindex |
| `AA_UPLOAD_BLOCKED_SUBCOLLECTIONS` | `academia_edu,us_gov_tech_reports,wikilib,aaaaarg,magzdb` | upload_records-Subcollections, die beim Import verworfen werden |
| `AA_UPLOAD_REQUIRE_TITLE_AUTHOR` | `true` | upload-Datensätze ohne echten Titel+Autor verwerfen |
| `AA_IA_REQUIRE_TEXTS` | `true` | Nur IA-Items mit mediatype „texts“ (Bücher/Scans) |
| `SYNC_REUSE_PREV_PAYLOAD` | `true` | Updates laden nur geänderte Torrent-Stücke (siehe Abschnitt Sync) |
| `SYNC_ENABLED` / `SYNC_SCHEDULE` | `true` / `03:15` | Täglicher inkrementeller Sync (Europe/Berlin) |
| `STORAGE_WARN_GIB` / `STORAGE_STOP_GIB` | `300` / `400` | Storage-Budget-Grenzen |

Dashboard & API sind unter Port **8010** im LAN erreichbar.

## 6. Docker Deployment

Drei Services in einem Compose-Stack:

* **postgres** – PostgreSQL 17, kein Host-Port, Healthcheck via `pg_isready`
* **api** – FastAPI (uvicorn), read-only rootfs, non-root, loopback-published
* **sync** – Scheduler-Worker (gleiche Image), schreibt nur nach `/work/sync`

Beide App-Container laufen mit `cap_drop: ALL`, `no-new-privileges`, begrenzten
Ressourcen und JSON-Log-Rotation (`max-size 10m`, `max-file 5`). Die API tritt
zusätzlich dem externen Netz `docker_bridge` bei, damit FE.Library sie per
Containername erreicht.

### Fortschritt im Browser (ohne Docker-CLI)

**`http://<server-ip>:8010/dashboard`** zeigt live (2 s während eines Syncs,
10 s im Leerlauf und reduziert bei inaktivem Browser-Tab):

* aktiver Sync: Phase (Download/Import/Validierung), Fortschrittsbalken,
  Zähler (gesehen/neu/aktualisiert/übersprungen/verworfen/Fehler)
* Collections-Übersicht mit letztem Release-Status
* Storage-Budget-Balken gegen Warn/Stopp-Grenzen
* die letzten Releases inklusive Fehlermeldungen

Technisch liest das Dashboard ausschließlich PostgreSQL (Tabelle
`sync_releases`), die der Sync-Worker während des Laufs aktualisiert – es
funktioniert damit containerübergreifend und ohne direkten Zugriff auf den
Worker. JSON-Endpoint für eigene Auswertungen: `/api/v1/sync/status`.

## 7. Bootstrap

Der erste Import lädt **nicht** automatisch bei `docker compose up`. Er ist ein
expliziter Befehl (unterbrichbar; bereits abgeschlossene Releases werden bei
Wiederholung übersprungen):

```bash
docker compose run --rm sync python -m sync.cli bootstrap
```

Hat das neueste Release einer Collection keine Seeder, kann ein älteres,
kumulatives Release gepinnt werden (`--release collection=suffix`, siehe
`docs/sync.md`); spätere Syncs laden nur das Delta.

Reihenfolge & Größen (komprimierte Downloads, Stand Aug 2026):

| Collection | Größe | Inhalt |
|---|---|---|
| `ia2_records` | ~3,5 GB | Internet Archive Items |
| `zlib3_records` | ~24 GB | Z-Library Bücher |
| `goodreads_records` | ~8 GB | Goodreads Metadaten (Anreicherung, keine Downloads) |
| `gbooks_records` | ~10 GB | Google Books Metadaten (Anreicherung, keine Downloads) |
| `libby_records` | ~6 GB | Libby/OverDrive Metadaten (Anreicherung, keine Downloads) |
| `upload_records` | ~146 GB | AA-Direktuploads (best-effort Metadaten) |

Die drei Anreicherungs-Quellen haben keine Datei-MD5s; sie erhalten einen
deterministischen synthetischen Schlüssel (`sha256(collection|id)`) und
verbessern Suche/Matching über ISBNs. Download-Referenzen liefert weiterhin
nur `/records/{md5}/sources` aus den Buch-Quellen.

## 8. Automatischer Sync

Der `sync`-Service prüft täglich um `SYNC_SCHEDULE` (Europe/Berlin) das
offizielle Manifest (`/dyn/torrents.json`), vergleicht mit der Tabelle
`sync_releases` und importiert nur neue kumulative Releases. Kein Full-Reimport.
Details: [docs/sync.md](docs/sync.md)

## 9. API

Basis: `http://<host>:8010/api/v1` (loopback) bzw. intern
`http://aa_metadata_api:8010/api/v1` (docker_bridge). Interaktiv:
`/docs`, Maschinenlesbar: `/openapi.json`.

```bash
# Suche mit Filtern
curl "http://127.0.0.1:8010/api/v1/search?q=harry+potter&language=de&extension=epub"

# Strukturierte Suche
curl "http://127.0.0.1:8010/api/v1/search?author=schätzing&year_from=2004"

# Identifier-Lookup (kein FTS nötig)
curl "http://127.0.0.1:8010/api/v1/search?isbn=9783161484100"
curl "http://127.0.0.1:8010/api/v1/search?doi=10.1000/182"

# Einzelner Record + Quellen-Referenzen
curl http://127.0.0.1:8010/api/v1/records/<32-hex-md5>
curl http://127.0.0.1:8010/api/v1/records/<32-hex-md5>/sources

# Systemstatus
curl http://127.0.0.1:8010/api/v1/status
curl http://127.0.0.1:8010/api/v1/health/live
curl http://127.0.0.1:8010/api/v1/health/ready
```

Antwortformat (Auszug):

```json
{
  "md5": "…",
  "title": "Example Book",
  "authors": ["Example Author"],
  "publisher": "Example Publisher",
  "publicationYear": 2020,
  "languages": ["deu"],
  "format": "epub",
  "filesize": 1234567,
  "identifiers": {"isbn13": ["978…"], "doi": [], "oclc": []},
  "workKey": "isbn:978…",
  "source": {"collection": "zlib3_records"}
}
```

Schutzmechanismen: `q` ≤ 200 Zeichen, `limit` 1–100 (Default 20), Cursor-
Pagination (kein OFFSET), DB-Statement-Timeout (5 s), validierte Parameter,
tombstoned Records (`deleted`) werden nie ausgeliefert.

## 10. FE.Library Integration

Siehe [docs/fe-library-integration.md](docs/fe-library-integration.md).
Kurzform: FE.Library erreicht den Dienst über das gemeinsame Docker-Netz
`docker_bridge` unter `http://aa_metadata_api:8010` – ohne Änderungen an
FE.Library selbst (nur eine neue Env-Variable + HTTP-Client im FE.Library-Code;
der dortige Änderungsbedarf ist im Dokument exakt beschrieben, aber nicht
durchgeführt).

## 11. Storage

* Budget-Modell: projizierte Gesamtbelegung = DB-Größe + ausstehende Downloads.
  Vor jedem Download/Import prüft der Storage-Guard und blockiert bei
  Überschreiten von `STORAGE_STOP_GIB` (Release wird als `blocked_storage`
  markiert, nichts wird beschädigt).
* Payloads (.zst) sind temporär: Nach erfolgreichem Import wird die Datei
  gelöscht. Nie dauerhaft Rohdaten neben der DB halten.
* Bericht: `python -m sync.cli storage-report` (Bytes/Record, Projektion @30M).

## 12. Backup

Die Datenbank ist vollständig aus den öffentlichen AA-Releases rekonstruierbar
(Bootstrap erneut ausführen). Deshalb:

* **Regelmäßig sichern:** die `docker-compose.yaml` (enthält das Passwort),
  Inhalt von `sync_releases` (optional, beschleunigt Rebuild-Markierung);
  Migrationen liegen in Git.
* **Kein lokales Full-DB-Backup** auf derselben Disk (würde Budget sprengen).
* Optionales externes Backup: `pg_dump -Fc` auf ein separates Ziel
  (siehe docs/architecture.md, Abschnitt Backup/Restore).

Nicht mehr aktive Quellen lassen sich kontrolliert aus dem Suchindex entfernen.
Der Befehl sperrt parallele Syncs, schreibt vor dem Löschen komprimierte
Binary-COPY-Backups nach `/work/sync/backup` und bereinigt auch die zugehörige
Release-Historie:

```bash
docker compose run --rm api python -m sync.cli purge-sources \
  --keep zlib3_records,upload_records --yes
```

## 13. Restore / Rebuild

Vollständiger Rebuild:

```bash
docker compose down -v
rm -rf "${DATA_HOST_DIR:-./data}/postgres"   # NAS-Pfad leeren
docker compose up -d
docker compose run --rm sync python -m sync.cli bootstrap
```

## 14. Troubleshooting

| Symptom | Ursache/Lösung |
|---|---|
| API `/health/ready` false | DB nicht erreichbar oder Migrationen pending → `docker compose logs api postgres` |
| Release status `blocked_storage` | Storage-Guard: `storage-report` prüfen, Thresholds anpassen oder Platz freigeben, dann `sync.cli retry <id>` |
| Torrent bleibt bei 0 % | Kaum Seeder (frisches Release): später erneut versuchen (`retry`) |
| `Another sync process holds the advisory lock` | Läuft bereits ein Sync (Bootstrap)? Geduld oder Container prüfen |
| Suche findet nichts | Noch kein Bootstrap ausgeführt? `db-stats` zeigt Bestand |

## 15. Update-Prozess

### Via Registry-Image (Standard – NAS-Deployment)

Der Stack läuft mit `ghcr.io/frederikemmer/aa-metadata-worker:latest`. Updates
erfolgen automatisch über Dockhand (geplante Auto-Updates) oder manuell:

```bash
docker compose pull          # neuestes Image laden
docker compose up -d         # rolling restart, Migrationen laufen beim API-Start
```

Bei `docker compose up -d` erkennt Compose den neuen Digest und startet die
Container neu. Migrationen laufen automatisch beim API-Start.

### Via lokalem Build (Entwicklung / Rollback)

```bash
docker compose -f docker-compose.local.yaml up -d --build
```

### CI/CD

Bei jedem Push auf `main` oder `v*`-Tag baut GitHub Actions das Image und
pushed es nach `ghcr.io/frederikemmer/aa-metadata-worker`. Plattform:
`linux/amd64` (NAS-kompatibel).

## 16. Tests

```bash
make setup    # einmalig
make unit     # Unit-Tests (offline)
make integration  # echte PostgreSQL-Instanz via Docker (Testcontainer)
make check    # alles inkl. Build & Compose-Validierung
```

Fixtures basieren auf offiziellen AAC-Sample-Datensätzen (aacid_small des
AnnaArchivist-Repos) – keine Multi-GB-Daten in Git.

## 17. Security

* PostgreSQL ist **nie** vom Host erreichbar (kein Port-Mapping).
* API published standardmäßig nur auf `127.0.0.1`; optional Bearer-Key via
  `METADATA_API_KEY`.
* Container: non-root, read-only rootfs, `cap_drop: ALL`,
  `no-new-privileges`, kein docker.sock.
* Das Passwort steht sichtbar in der `docker-compose.yaml` (bewusste Entscheidung
  für Heimserver-Betrieb) – Repo daher nicht öffentlich pushen.
  Optionaler Bearer-Schutz via `METADATA_API_KEY`. Keine Buchdateien/-Downloads im Scope.
* Dependencies & Base Images sind gepinnt (`requirements*.txt`, Dockerfile).

## 18. Bekannte Einschränkungen

* `upload_records` enthält viele Nicht-Buch-Subcollections; Metadaten sind
  best-effort (exiftool/filename) und oft dünn.
* Download der Payloads erfordrt funktionierenden BitTorrent-Traffic
  (eingebetteter libtorrent-Client).
* Resume-Granularität ist Release-Level: Ein unterbrochener Release-Import
  wird komplett neu durchlaufen (idempotent dank Merge-on-conflict).
* FTS ist sprachneutral (`simple`); semantische Suche/Stemming bewusst nicht
  Teil von v1.
* Keine Prometheus-/Metrics-Endpoint (Infrastruktur nutzt Uptime-Kuma/Glance);
  `/api/v1/status` deckt Betriebsdaten ab.
