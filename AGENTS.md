# AA Metadata Worker – Agent Hub

Landing page für Contributors und Coding Agents. Primäre Navigation: `/docs`.

## Project Goal

Eigenständiger Metadaten- und Suchdienst: importiert Anna's-Archive-Container
(AAC) lokal, hält sie inkrementell aktuell (PostgreSQL + FTS) und stellt sie
FE.Library über eine stabile REST-API (`/api/v1`) bereit.

## Non-Negotiables

1. **Metadata-only.** Keine Buchdateien herunterladen, hosten, streamen oder
   Mirror-/Download-Resolver bauen. Quellen-Referenzen
   (`/records/{md5}/sources`) sind die Grenze.
2. **Kein Web-Scraping** von Anna's Archive; ausschließlich offizielle
   Metadata-Torrents/Manifeste (`/dyn/torrents.json`).
3. **Andere Projekte unter ~/CODE/ strikt read-only** – insbesondere `fe-library`.
4. Schemaänderungen **nur** über neue Migrationen (`migrations/NNN_*.sql`,
   Versionsnummer strikt steigend, bereits angewendete nie editieren).
5. Parameterized SQL only.
6. Indexierung und Query-Normalisierung müssen dieselben Funktionen in
   `common/normalize.py` nutzen – niemals auseinanderleben lassen.
7. Merge-Strategie nur in `common/records.merge_records` ändern (Tests!).
8. Keine neuen Indizes ohne gemessenen API-Nutzungsfall (Storage!).
9. Buch-Filter (uploads gate/blocklist, ia2 mediatype) sind Absicht – nicht
   entfernen; Erweiterungen konfigurierbar über Settings halten.
10. Keine `.env` wieder einführen: Konfiguration lebt kommentiert in der
    `docker-compose.yaml`.

## Architecture Map

```text
app/       FastAPI (/api/v1): health, status, search, records, sources
common/    config, normalize (ISBN/DOI/Sprache/Text), records+merge, db+migrations
sync/      discovery, torrent_client, importer (streaming), state, storage_guard,
           run (Orchestrierung), worker (Scheduler), cli
migrations/ 0001_init.sql …
tests/     unit | integration (echtes PG via Docker) | api | fixtures
```

## Commands

```bash
make setup          # venv + deps
make check          # lint + unit + integration + compose config + docker build
docker compose up -d                                        # Basisstack
docker compose run --rm sync python -m sync.cli bootstrap   # Erstimport (explizit!)
docker compose run --rm sync python -m sync.cli status      # Sync-State
```

Dashboard: `http://<host>:8010/dashboard` (liest nur `sync_releases`).
Konfiguration erfolgt ausschließlich über `docker-compose.yaml` (bewusst kein .env).

## Conventions

* Python 3.11, PEP8 + Type Hints, ruff (`line-length=110`), pytest markers
  (`unit`, `integration`, `api`, `sync`, `slow`).
* Logging strukturiert auf Release-Ebene, niemals pro Record.
* Commit messages: `feat: …`, `fix: …`, `docs: …`.
* Docs aktualisieren, wenn Verhalten/APIs/Datenmodell sich ändern.

## Definition Of Done

1. Tests (unit + relevante Integration) grün, `make check` erfolgreich.
2. Migrationen idempotent & versioniert.
3. Storage-Budget respektiert; neue Features prüfen Guard-Interaktion.
4. Docs (README, docs/*) aktualisiert.
5. Keine Änderungen außerhalb dieses Projekts.
