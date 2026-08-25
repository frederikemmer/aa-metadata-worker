# Sync-Dokumentation

## Metadata Discovery

Quelle ist ausschließlich das offizielle AAC-Torrent-Manifest:

```text
GET {AA_MIRROR_BASE_URL}/dyn/torrents.json
```

Relevante Einträge sehen so aus (gekürzt):

```json
{
  "display_name": "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent",
  "is_metadata": true, "obsolete": false,
  "btih": "7674860ae1cc4c23d61842b4aba617e1972700e6",
  "url": "https://…/dyn/small_file/torrents/managed_by_aa/annas_archive_meta__aacid/<name>.torrent",
  "magnet_link": "magnet:?xt=urn:btih:…",
  "data_size": 23950000000
}
```

Auswahlregel pro Collection: unter allen `is_metadata && !obsolete`-Torrents
dasjenige mit dem größten End-Timestamp (`--<to>`). Kumulative Range-Releases
superseden ältere; bereits importierte Records sind in neueren Releases
byte-identisch enthalten, deshalb genügt „importiere das neueste Release“ als
inkrementeller Update.

## Download

* Die `.torrent`-Datei wird per HTTPS geladen, die Payload per BitTorrent
  (eingebetteter libtorrent-Client im sync-Container).
* Payload landet in `sync_work` (Volume), wird nach erfolgreichem Import
  gelöscht. Temporärer Platzbedarf = komprimierte Releasegröße.
* Kein Web-Scraping, keine Browser-Automatisierung, keine Anti-Bot-Umgehung.

## Buch-Filterung beim Import („nur Nützliches behalten“)

Nicht-Buch-Material wird bereits **während des Imports verworfen** und landet
gar nicht in der Datenbank:

| Collection | Filter | Konfiguration |
|---|---|---|
| `upload_records` | Subcollection-Blocklist (academia_edu, us_gov_tech_reports, wikilib, aaaaarg, magzdb) | `AA_UPLOAD_BLOCKED_SUBCOLLECTIONS` |
| `upload_records` | Datensätze ohne echten Titel+Autor (aus exiftool/pikepdf – Dateinamen-Fallback zählt nicht) werden verworfen | `AA_UPLOAD_REQUIRE_TITLE_AUTHOR` |
| `ia2_records` | Nur IA-Items mit mediatype „texts“; Audio/Video/… wird verworfen | `AA_IA_REQUIRE_TEXTS` |

Verworfene Datensätze werden pro Release gezählt (`records_discarded`) und sind
im Dashboard sichtbar.

## Incremental Updates

```text
Manifest laden
   ↓ für jede AA_COLLECTIONS-Collection:
neuestes Release ermitteln
   ↓ bereits 'completed' in sync_releases? → fertig (skip)
Storage-Guard prüfen
   ↓
Download (status=downloading)
   ↓
Stream-Import (status=importing)
   ↓
Validierung (status=validating): Records gesehen? Fehlerquote ok?
   ↓
completed + Payload löschen
```

Ein zweiter Lauf importiert dasselbe Release nie erneut (Vergleich über
`release_identifier`). Da kumulative Releases alte Records byte-identisch
enthalten, deckt der neueste Import immer den kompletten Bestand ab.

### Delta-Downloads: nur das Neue herunterladen (`SYNC_REUSE_PREV_PAYLOAD=true`)

AAC-Releases wachsen kumulativ: Ein neues zlib3-Release unterscheidet sich vom
Vorgänger nur um die seitdem neu hinzugekommenen Records. Da t2sz-Blöcke (10 MB,
festes Level) deterministisch komprimieren und Records append-only sind, ist der
komprimierte Prefix zweier Releases **byte-identisch**. Genutzt wird das so:

1. Nach erfolgreichem Import bleibt der Payload als Seed-Basis erhalten
   (`sync_work/.prev/<collection>.payload`).
2. Beim nächsten Release wird diese Datei per Hardlink unter dem neuen
   Dateinamen verlinkt (keine Kopie, kein Extra-Speicher).
3. libtorrent hash-checkt alle Pieces: alles im identischen Prefix gilt als
   vorhanden, **nur die geänderten/neuen Pieces werden geladen** (typ. wenige
   hundert MB statt 24/146 GB).

Korrektheit hängt nie von der Optimierung ab: Stimmt ein Piece nicht (weil AA
z. B. Kompressionsparameter ändert), wird es einfach regulär geladen – im
Schlechtestfall entspricht es einem vollen Download. Kosten der Optimierung:
der vorherige Payload bleibt auf der Platte (+24/+146/+3,5 GB je Collection);
bei knappe Storage `SYNC_REUSE_PREV_PAYLOAD=false` setzen.

## Retry

```bash
# fehlgeschlagene/blocked Releases auflisten
docker compose exec sync python -m sync.cli status

# einzelnen Release erneut anstoßen
docker compose run --rm sync python -m sync.cli retry <release_id>

# gesamten Durchlauf wiederholen (überspringt completed automatisch)
docker compose run --rm sync python -m sync.cli run
```

## Recovery / Crash-Sicherheit

| Fall | Verhalten |
|---|---|
| Crash während Downloads | Status bleibt `downloading`; nächster Lauf setzt zurück und lädt neu. Vorhandene Teile nutzt libtorrent beim Re-Add (Re-check) wieder. |
| SIGTERM während Import | Signal-Handler beendet zwischen zwei Batches; Batch-Transaktion war atomar; Release → `discovered` (+ Meldung „resumable“). |
| Crash während Import | Status bleibt `importing`; `run`/`retry` startet den Release-Import neu. Idempotent durch Merge-on-conflict: keine Duplikate. |
| Defekter Record | Zählt auf `records_failed`, max. 20 Samples im Log; Import läuft weiter. |
| Hohe Fehlerquote (> SYNC_ERROR_ABORT_RATE) | Release bricht ab → `failed`. |
| Paralleler Start zweier Syncs | PostgreSQL Advisory Lock: der zweite Prozess beendet sich mit `SyncLockBusy`. |

## Cleanup

* Payload-Dateien werden unmittelbar nach `completed` bzw. bei `failed` gelöscht.
* Bleibt nach einem Absturz eine Datei liegen, erkennt sie der nächste
  Download-Versuch desselben Releases (libtorrent Re-check) oder sie kann manuell
  aus `${DATA_HOST_DIR}/sync_work` entfernt werden.

## Storage Protection

Budget-Modell: `projizierte Gesamtbelegung = pg_database_size() +
ausstehende Bytes`. Vor jedem Download **und** nochmal vor dem Import:

* ≥ `STORAGE_STOP_GIB` oder zu wenig physisch freier Platte → Operation startet
  nicht; Release → `blocked_storage`; bestehende Daten bleiben unberührt.
* ≥ `STORAGE_WARN_GIB` → Warnung im Log, Import läuft weiter.

Annahme: `postgres/` und `sync_work/` liegen unter demselben
`DATA_HOST_DIR` (dasselbe Dateisystem/NAS) – dann spiegelt der freie Platz im
sync-Container die real verfügbare Kapazität wider.

Bericht jederzeit: `python -m sync.cli storage-report`.

## Release Tracking

Alle Kennzahlen je Release liegen in `sync_releases` (siehe docs/data-model.md):
Bytes, Zeilen, inserted/updated/skipped/failed, Zeitstempel, Fehlermeldung. Die
API exponiert den aktuellen Zustand über `/api/v1/status`
(`lastSuccessfulSync`, `sync.status`, `sync.activeRelease`).

## Bootstrap

```bash
docker compose up -d            # Basisdienste
docker compose run --rm sync python -m sync.cli bootstrap
```

* Läuft Collection für Collection (Release für Release, nicht alle parallel).
* Unterbrechbar (Ctrl-C/SIGTERM): einfach erneut starten – fertige Releases
  werden übersprungen, laufende werden sauber zurückgesetzt.
* Lädt niemals automatisch beim Start des Stacks.

### Bestimmten Release pinnen (--release)

Standardmäßig wird immer das neueste Release einer Collection importiert. Hat
das neueste Release (noch) keine Seeder, kann der Bootstrap/Run explizit auf ein
älteres, kumulatives Release gepinnt werden – alle seekbaren Releases teilen
einen byte-identischen Prefix; spätere Syncs laden nur das Delta:

```bash
python -m sync.cli bootstrap \
  --release zlib3_records=--20260706T193143Z.jsonl.seekable.zst \
  --release upload_records=--20260412T185511Z.jsonl.seekable.zst
```

Der Suffix muss das Release-Identifier-Ende eindeutig identifizieren; bei 0 oder
mehreren Treffern bricht der Lauf vor dem Download mit Fehler ab.
