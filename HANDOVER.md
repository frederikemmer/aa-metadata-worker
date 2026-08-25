# Übergabedokument – aa-metadata-worker

> Stand: 25.08.2026. Erstellt nach interaktiver Session mit laufender Produktion auf dem NAS.
> **Update 25.08.2026 16:00**: Alle Aufgaben A1–A4 + A6 abgeschlossen. A5 (Dockhand)=User-Aktion.
> **AGENTS.md im Repo-Stamm gilt weiterhin unverändert** (Non-Negotiables!).

---

## Status der Aufgaben

| Auftrag | Status | Details |
|---|---|---|
| A1 – GitHub-Repo + CI | ✅ erledigt | `frederikemmer/aa-metadata-worker` (public), CI pushed nach `ghcr.io` |
| A2 – docker-compose.yaml Registry | ✅ erledigt | `image: ghcr.io/frederikemmer/aa-metadata-worker:latest`, `API_STATEMENT_TIMEOUT_MS=20000` |
| A3 – NAS migriert | ✅ erledigt | Stack von `docker-compose.local.yaml` → `docker-compose.yaml` gewechselt |
| A4 – Bootstrap gestartet | ✅ gestartet | `aa_bootstrap` Container läuft (zlib3 + upload) |
| A5 – Dockhand Auto-Update | ⏳ User-Aktion | Siehe Abschnitt unten |
| A6 – Status-500 Fix | ✅ erledigt | `approx_count()` mit reltuples-Fallback + 20s Timeout |
| A7 – Docs aktualisiert | ✅ erledigt | README Abschnitt 15 aktualisiert |

---

## 1. Aktueller Auftrag des Users

Der User betreibt den aa-metadata-worker produktiv auf seinem Ugreen NAS und möchte:

1. **Von unterwegs über Dockhand updaten können** (Dockhand läuft bereits auf dem NAS, Port 3003).
2. **Automatische Updates erhalten, sobald ein neues Image gepushed wird.**

Dazu wurde begonnen, das Deployment von „lokalem Build" auf „Registry-Image (ghcr.io) + CI"
umzustellen. **Diese Umstellung ist vorbereitet, aber NICHT abgeschlossen**
(siehe Abschnitt 6: offene Aufgaben).

---

## 2. Systemzugänge & Besonderheiten des NAS

| Was | Wert |
|---|---|
| SSH | `ssh Frederik@fe.local` |
| Passwort | `Ugreen,99` |
| sudo | gleiches Passwort (`echo 'Ugreen,99' \| sudo -S <cmd>`) |
| Projektverzeichnis auf NAS | `/volume3/docker/AA Metadata Worker` (Leerzeichen im Pfad!) |
| Docker-Zugriff | nur via sudo (User ist nicht in der docker-Gruppe) |
| Dashboard/API | http://fe.local:8010/dashboard |
| Postgres (Container) | `aa_metadata_postgres_local`, DB `aa_metadata`, User `aa_metadata`, PW `change-me-please` |

**Regeln auf dem NAS (User-Vorgabe, strikt einhalten):**

- Nur im Projektverzeichnis `/volume3/docker/AA Metadata Worker` und an den
  aa_metadata-Containern arbeiten.
- NICHTS anderes am System verändern/löschen/bearbeiten.
- Andere Projekte unter `~/CODE/` lokal sind read-only (fe-library!).

**UGOS-Eigenheiten (wichtig!):**

- `rsync` und `scp` zum NAS sind kaputt (UGOS ersetzt rsync durch einen eigenen Daemon;
  scp scheitert am Leerzeichen im Pfad). → Dateiübertragung immer per tar-Pipe:

```bash
tar cz - --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '*.pyc' --exclude '.DS_Store' . \
  | ssh Frederik@fe.local "tar xzf - -C '/volume3/docker/AA Metadata Worker'"
```

  (Warnings `LIBARCHIVE.xattr` beim Entpacken sind harmlos.)

- Der SSH-Dienst kann vom System beendet werden (passierte zweimal). Symptom: Port 22
  „Connection refused", während HTTP (8010) weiterhin antwortet. Der User muss SSH dann in
  der UGOS-Weboberfläche wieder aktivieren.
- Container-User im App-Image ist `metadata` mit **UID/GID 999**. Die Host-Verzeichnisse
  `data/postgres` und `data/sync_work` gehören UID 999 – **nicht ändern!**

---

## 3. Produktionsstand (verifiziert)

### Container (Variante: lokaler Build)
Gestartet über `docker-compose.local.yaml` (baut Image aus dem Verzeichnis, pull_policy never):

- `aa_metadata_postgres_local` (postgres:17.6-bookworm)
- `aa_metadata_api_local` (Port 8010)
- `aa_metadata_sync_local` (Worker, SYNC_SCHEDULE=03:15 Europe/Berlin)

### Datenbestand

- `metadata_records`: **24.889.217 Datensätze**
- Collections:
  - `ia2_records`: **COMPLETED** ✅ (Release 20240126T065114Z--20260626T041035Z,
    5.486.592 gesehen, 0 Fehler; Payload als Seed-Basis unter
    `data/sync_work/.prev/ia2_records.payload`, ~3,4 GiB)
  - `zlib3_records`: FAILED ❌ – **neuestes Release hat 0 Seeder** (per UDP-Tracker-Scrape verifiziert)
  - `upload_records`: FAILED ❌ – gleiche Ursache (neuestes Release vom 05.08.: 0 Seeder)

### Seeder-Lage (UDP-Scrape tracker.opentrackr.org, Stand 25.08.)

| Collection | neuestes Release | Seeder | Alternative (gepinnt) | Seeder |
|---|---|---|---|---|
| zlib3_records | …--20260821T041731Z (22,3 GiB) | **0** | `--release zlib3_records=--20260706T193143Z.jsonl.seekable.zst` (22,2 GiB) | **15** |
| upload_records | …--20260805T193618Z (136 GiB) | **0** | `--release upload_records=--20260412T185511Z.jsonl.seekable.zst` (131 GiB) | **4** |

Alle kumulativen Releases einer Collection teilen einen byte-identischen Prefix → Bootstrap vom
älteren Release ist okay; spätere Syncs laden nur das Delta (Hardlink + Hashcheck).

---

## 4. In dieser Session behobene Bugs (188 Tests grün)

1. **Berechtigungen**: `data/sync_work` gehörte root → Sync-Container (UID 999) konnte nichts
   schreiben (`PermissionError /work/sync/.prev`). Fix: `sudo chown -R 999:999 data/sync_work`.
2. **libtorrent 2.x-Kompatibilität** (`sync/torrent_client.py`):
   - `session.abort()` existiert nicht mehr → nur bedingt aufrufen (`getattr`).
   - `remove_torrent(handle, delete_files=False)` wirft ArgumentError → `remove_torrent(handle)`
     ohne Keyword (Default behält Dateien).
3. **UnboundLocalError** in `sync/run.py`: `client` war erst im Loop initialisiert; ein Fehler
   davor verschlang den Original-Fehler. Fix: Init vor den try-Block gezogen.
4. **Parser-Bug IA-Listenfelder** (Hauptursache des ia2-Importabbruchs mit 2 % Fehlerrate):
   IA-Metadaten enthalten teils Listen statt Strings (`title`, `publisher`, `creator`,
   `mediatype`, `language`) → `AttributeError: 'list' object has no attribute 'strip'`.
   - Fixes: `sync/sources/ia2.py` (`_as_text`, Autoren über `_as_list`),
     `sync/sources/zlib3.py` (`_scalar_text`), `common/languages.py`
     (`language_to_iso639_3` akzeptiert Listen), Type-Hint `normalize_language`.
   - Danach lief der ia2-Import mit **0 Fehlern** durch (vorher Abbruch bei 876/42.928).
5. **Beobachtbarkeit**: `sync/importer.py` loggt jetzt bis zu 5 Fehlermuster pro Release
   (`_log_error_samples`), wenn die Fehlerrate überschritten wird.
6. **`--release`-Override** (neues Feature): Bootstrap/Run kann einen Release pinnen:

```bash
python -m sync.cli bootstrap \
  --release zlib3_records=--20260706T193143Z.jsonl.seekable.zst \
  --release upload_records=--20260412T185511Z.jsonl.seekable.zst
```

   Implementiert in `sync/discovery.py` (`find_release`), `sync/run.py`
   (`release_overrides`-Parameter), `sync/cli.py` (`--release COLLECTION=SUFFIX`, repeatable).
7. **Dashboard-Steuerung** (früher gebaut, in Produktion verifiziert): Buttons
   „Jetzt synchronisieren" / „Pause/Fortsetzen", API `/api/v1/sync/commands` +
   `/api/v1/sync/control`, Command-Poller im Worker, Migration `0004_sync_control.sql`.

**Wichtig**: Der maßgebliche Code liegt LOKAL in `~/CODE/aa-metadata-worker`. Auf dem NAS liegt
eine Kopie (per tar übertragen). **Das lokale Git-Repo hat noch KEINEN Commit!**

## 5. Bekannte offene Probleme

### 5.1 `/api/v1/status` und `/api/v1/sync/status` → HTTP 500

**Ursache**: API-Connectionpool hat `statement_timeout = 5000 ms` (Default aus
`common/db.py::get_pool`, Env `API_STATEMENT_TIMEOUT_MS`). `SELECT COUNT(*) FROM metadata_records`
braucht nach dem Bulk-Import >10 s (stale Visibility Map; EXPLAIN zeigte ~10 s
Parallel Index Only Scan bei 24,9 M Rows). Search-Endpoint funktioniert normal.

**Geplanter Fix (noch nicht implementiert):**

a) **Sofortmaßnahme** in Compose-Datei:
   `API_STATEMENT_TIMEOUT_MS: "20000"` im api-Service.

b) **Codefix**: Bei psycopg-Timeout (SQLSTATE 57014 / `QueryCanceled`) Fallback auf
   Planner-Schätzung:
   `SELECT GREATEST(reltuples,0)::bigint FROM pg_class WHERE oid='metadata_records'::regclass`
   Betroffene Stellen:
   - `app/routes/status.py` (~Zeile 26) – `SELECT COUNT(*) FROM metadata_records`
   - `app/routes/dashboard.py` `sync_status()` (~Zeile 66) – `records_row`

c) **Optional**: `VACUUM ANALYZE metadata_records;` im Postgres-Container ausführen
   (beschleunigt COUNT dauerhaft).

### 5.2 Bootstrap für zlib3 + upload fehlt noch
Siehe Aufgabe A4 unten.

---

## 6. Offene Aufgaben (Reihenfolge einhalten!)

### A1 – GitHub-Repo + CI erstellen

- `gh` CLI ist authentifiziert als `frederikemmer` (Token im Keyring).
- Repo `frederikemmer/aa-metadata-worker` existiert noch nicht → erstellen
  (**public**, damit das NAS ohne Registry-Auth pullen kann).
- Workflow `.github/workflows/docker-image.yml` anlegen:
  - Trigger: push auf `main`, Tags `v*`, `workflow_dispatch`
  - permissions: `contents: read`, `packages: write`; GITHUB_TOKEN für ghcr-Login
  - `docker/build-push-action@v6`, Platform **linux/amd64** (NAS ist Intel/x86_64)
  - Tags: `latest` (default branch) + `sha-<hash>` + SemVer aus v-Tags
  - Image: `ghcr.io/frederikemmer/aa-metadata-worker`
  - Cache: `type=gha`
- Lokales Repo committen (erster Commit überhaupt! Konventionen: `feat: …` etc.)
  und pushen. `HANDOVER.md` nicht vergessen.
- CI-Run beobachten (`gh run watch`), prüfen dass Package auf ghcr.io erscheint.

### A2 – docker-compose.yaml auf Registry-Image umstellen

- In `docker-compose.yaml` (Standard-Variante): bei `api` und `sync` die `build:`-Blöcke
  entfernen und `image: ghcr.io/frederikemmer/aa-metadata-worker:latest` setzen.
  Postgres unverändert lassen.
- `API_STATEMENT_TIMEOUT_MS: "20000"` im api-Service ergänzen (gegen Status-500).
- `docker-compose.local.yaml` als Build-Variante/Rollback behalten.

### A3 – NAS migrieren (nach A1 + A2!)

1. Aktualisierten Code + Compose per tar-Pipe aufs NAS übertragen (Befehl in Abschnitt 2).
2. Alte Stack stoppen (Volumes sind Bind-Mounts → bleiben erhalten!):
   ```
   sudo docker compose -f '/volume3/docker/AA Metadata Worker/docker-compose.local.yaml' down
   ```
3. Neuen Stack starten:
   ```
   sudo docker compose -f '/volume3/docker/AA Metadata Worker/docker-compose.yaml' up -d
   ```
   - Pullt ghcr-Image; neue Containernamen **ohne** `_local`-Suffix.
   - Gleiche `data/`-Mounts → kein Datenverlust.
4. Verifizieren:
   - `curl http://fe.local:8010/api/v1/health/live` → `{"status":"ok"}`
   - Dashboard unter http://fe.local:8010/dashboard laden
   - `curl http://fe.local:8010/api/v1/search?q=test` → Treffer
   - Worker-Log zeigt „Next sync in …"
5. Postgres-Container heißt jetzt `aa_metadata_postgres` (ohne `_local`) –
   psql-Befehle entsprechend anpassen.

### A4 – Bootstrap zlib3 + upload starten (NACH A3!)

```bash
sudo docker compose -f '/volume3/docker/AA Metadata Worker/docker-compose.yaml' run -d \
  --name aa_bootstrap sync python -m sync.cli bootstrap \
  --release zlib3_records=--20260706T193143Z.jsonl.seekable.zst \
  --release upload_records=--20260412T185511Z.jsonl.seekable.zst
```

- ia2 wird automatisch übersprungen (Status: completed).
- Download ~153 GiB gesamt → mehrere Stunden; Fortschritt im Dashboard sichtbar.
- Vor Start ggf. Seeder-Zahl erneut prüfen (kann sich täglich ändern); falls Juli-Release
  inzwischen 0 Seeder hat, Scrape wiederholen und alternativen Release pinnen.
- Storage-Guard: Budget 300 GiB (Warn) / 400 GiB (Stop) ist konfiguriert und deckt das ab.

### A5 – Auto-Update über Dockhand

- Dockhand (fnsys/dockhand, Port 3003) unterstützt native geplante Auto-Updates pro
  Stack/Container (inkl. Vulnerability-Scan vor dem Wechsel, „safe-pull").
- Watchtower ist NICHT nötig.
- User muss Auto-Update in der Dockhand-UI aktivieren (Login hat nur der User).
  Alternativ kann der Agent die UI steuern, wenn der User Dockhand-Zugang gibt.
- Bild-Tag `:latest` in Compose reicht – Dockhand erkennt den neuen Digest automatisch.

### A6 – Status-500 endgültig fixen (siehe 5.1)

- Codefix mit Fallback implementieren (reltuples-Schätzung bei Timeout).
- API-Test schreiben (`tests/api/`): Mock- oder Integrationstest, der Verhalten bei
  Statement-Timeout prüft.
- Deployen und Verifizieren.

### A7 – Docs aktualisieren

- README: Deployment-Abschnitt für Registry-Setup, CI, Dockhand-Auto-Update.
- HANDOVER.md nach Abschluss löschen oder als archiviert markieren.

---

## 7. Rollback-Plan

Vorherige Variante bleibt funktionsfähig:
`docker-compose.local.yaml` + lokales Image `aa-metadata-worker:local` existieren
auf dem NAS weiter. Rollback = neuen Stack down, alten wieder hochfahren.
Bind-Mounts identisch → keine Datenrisiken.

---

## 8. Nützliche Snippets

```bash
# Status / Control prüfen
curl -s http://fe.local:8010/api/v1/sync/control | python3 -m json.tool
curl -s http://fe.local:8010/api/v1/sync/status | python3 -m json.tool

# Worker-Logs (Container-Name ggf. nach A3 anpassen)
echo 'Ugreen,99' | ssh Frederik@fe.local "echo 'Ugreen,99' | sudo -S docker logs aa_metadata_sync_local --tail 50"

# Postgres-Direktzugriff (nach A3: aa_metadata_postgres ohne _local)
echo 'Ugreen,99' | ssh Frederik@fe.local "echo 'Ugreen,99' | sudo -S docker exec aa_metadata_postgres_local psql -U aa_metadata -d aa_metadata -c 'SELECT COUNT(*) FROM metadata_records;'"

# Pinned Bootstrap Container stoppen (wenn noetig)
sudo docker stop aa_bootstrap && sudo docker rm aa_bootstrap

# Dump-Fix (falls Dump schon einmal angelegt wurde):
#   data/postgres/dump umkopieren/loeschen, dann Postgres neu starten
echo 'Ugreen,99' | ssh Frederik@fe.local "echo 'Ugreen,99' | sudo -S docker restart aa_metadata_postgres_local"
```

---

## 9. Technische Details

- Python 3.11 im Image, lokales Dev: 3.13. ruff line-length=110.
- Postgres 17.6 mit `shared_preload_libraries = pg_trgm`.
- sync.run_mode: Nur `bootstrap` oder `sync` gleichzeitig (advisory lock).
- Sync cron: `03:15 Europe/Berlin` (kein tzdata nötig; dt.py hält IntlDateTimeFormat-
  Workaround, NICHT `zoneinfo.ZoneInfo` benutzen → Split-Brain-Error in Docker).
- Tracker Scrape (UDP, `tracker.opentrackr.org`): Im Channel `session/torrent_client.py`
  wurde ein funktionierendes Minimalbeispiel geteilt – kann bei Bedarf für Seed-Checks
  wiederverwendet werden.
- `AGENTS.md` (Projekt-Regeln) im Repo-Stamm: Non-Negotiables (kein File-Hosting,
  kein Web-Scraping von AA, migrations append-only, parameterized SQL, etc.).

---

*Erstellt von opencode am 25.08.2026 als Übergabe an nachfolgenden Agent.*
