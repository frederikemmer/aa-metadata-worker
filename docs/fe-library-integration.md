# FE.Library Integration – AA Metadata Worker

Diese Dokumentation beschreibt, wie **FE.Library** den Metadata-Dienst nutzt.
FE.Library selbst wurde im Rahmen dieses Projekts **nicht verändert**; der
Abschnitt „Änderungsbedarf in FE.Library“ beschreibt exakt und ausschließlich,
was dort später zu ergänzen wäre.

## Grundlagen (aus der read-only Analyse von FE.Library)

* Backend: **Python 3.11 + FastAPI**, HTTP-Clients mit **httpx** (bereits in
  `requirements.txt`), Konfiguration über Environment-Variablen
  (`METADATA_AI_BASE_URL`, `METADATA_CONTACT_EMAIL`, …).
* Sprachcodes: FE.Library normalisiert intern auf **ISO-639-3** – dieser Dienst
  liefert Sprachen bereits als ISO-639-3 (`deu`, `eng`, …). Keine Konvertierung nötig.
* Deployment: Container `fe_library` published Host-Ports (8008/8009) und liegt
  im Default-Bridge-Netz; die Server-Infrastruktur betreibt zusätzlich das
  externe Netz `docker_bridge` (Zoraxy, Glance, …).

## Base URL & Docker Networking (Variante A)

Der Metadata-Stack tritt dem externen Netz `docker_bridge` bei. Damit FE.Library
den Dienst erreicht, muss der `fe_library`-Container **demselben Netz beitreten**
– das ist die einzige Änderung auf FE.Library-Seite der *Betrieb* betrifft
(eine Zeile in dessen Deploy-Konfiguration, nicht im Code-Repository):

```yaml
# FE.Library deploy compose (nur ergänzen):
services:
  fe_library:
    networks:
      - default          # bestehend
      - docker_bridge    # neu: externes Netz

networks:
  docker_bridge:
    external: true
```

Base URL im Container:

```env
METADATA_API_URL=http://aa_metadata_api:8010
```

Alternativen: Loopback-Publish (`http://127.0.0.1:8010` vom Host aus, bereits
aktiv) oder eine Zoraxy-Route – beide ohne FE.Library-Netzänderung, aber der
Containerpfad ist am einfachsten und sichersten (Traffic verlässt den Host nie).

## Environment Variable (Vorschlag passend zur FE.Library-Konvention)

```env
# .env von FE.Library
METADATA_API_URL=http://aa_metadata_api:8010
METADATA_API_KEY=            # optional; nur setzen, wenn aa-metadata-worker
                             # einen Key via METADATA_API_KEY erfordert
METADATA_API_TIMEOUT=10      # Sekunden, siehe Timeouts
```

Namensschema folgt dem vorhandenen Muster `METADATA_*`
(`METADATA_AI_BASE_URL`/`METADATA_AI_API_KEY`).

## Authentication

Standardmäßig ungeschützt (Loopback-/interner Traffic). Wenn der Metadata-Dienst
einen Key gesetzt hat, jeden Request um einen Header erweitern:

```http
Authorization: Bearer <METADATA_API_KEY>
```

## API-Endpunkte für FE.Library

| Zweck | Endpoint |
|---|---|
| Suche | `GET /api/v1/search?q=&title=&author=&isbn=&doi=&language=&extension=&year_from=&year_to=&limit=&cursor=` |
| Record-Detail | `GET /api/v1/records/{md5}` |
| Quellen-Referenzen | `GET /api/v1/records/{md5}/sources` |
| Verfügbarkeit | `GET /api/v1/status` |
| Health | `GET /api/v1/health/live` · `/health/ready` |

### Search Response Type

```jsonc
{
  "totalLowerBound": 42,
  "limit": 20,
  "nextCursor": "eyJyIjoiMC4xMjM0IiwibSI6IjAxMmM…",   // null = Ende
  "results": [
    {
      "md5": "0123456789abcdef0123456789abcdef",
      "title": "Example Book",
      "authors": ["Jane Doe"],
      "publisher": "Pub House",
      "publicationYear": 2020,
      "languages": ["deu"],                 // ISO-639-3 wie FE.Library
      "format": "epub",
      "filesize": 1234567,
      "identifiers": {
        "isbn10": [], "isbn13": ["9783161484100"], "doi": [],
        "oclc": [], "openlibrary": []
      },
      "workKey": "isbn:9783161484100",      // logische Werk-ID zum Gruppieren
      "source": {"collection": "zlib3_records", "record_id": "22433983", "aacid": "aacid__…"}
    }
  ]
}
```

Hinweise:

* **Keine Download-URLs** im Suchergebnis. Für Beschaffungsreferenzen dient
  `GET /records/{md5}/sources` → `{aaPageUrl, ipfsCid?, source}`; FE.Library
  entscheidet selbst, ob/wie es damit umgeht (Auftrag: keine Content-Auflösung
  im Metadata-Dienst).
* `language=de` wird akzeptiert und serverseitig nach `deu` normalisiert;
  FE.Library kann direkt 2- oder 3-letter Codes senden.
* ISBN-Eingaben dürfen Bindestriche enthalten (`978-3-…`); invalid → `400`.
* Pagination: `nextCursor` transparent weiterreichen; niemals eigene Offsets bauen.

## Timeout-Empfehlungen

| Call | Timeout | Begründung |
|---|---|---|
| `/search` | 5–8 s | DB-Statement-Timeout serverseitig 5 s; p95 bleibt typ. darunter |
| `/records/{md5}` | 3 s | PK-Lookup |
| `/status` | 3 s | Counters/Größen |

Verbindungsfehler/Timeouts sollen FE.Library wie andere optionale externe
Dienste behandeln: degradieren (Suche „im Web suchen“ bleibt ja vorhanden),
nicht crashen.

## Fehlerbehandlung

| Status | Bedeutung | Empfohlenes Verhalten |
|---|---|---|
| 400 | Ungültige Parameter (z. B. ISBN/MD5/Cursor) | Eingabe validieren, Nutzerhinweis |
| 404 | MD5 unbekannt | „Nicht im Index“ |
| 401 | Bearer-Key falsch/fehlend (falls konfiguriert) | Config prüfen |
| 410 | Record wurde an der Quelle entfernt | UI: nicht mehr verfügbar |
| 422 | Query-Limit überschritten (FastAPI-Validierung) | Parameter kürzen |
| 5xx/Timeout | Dienst nicht bereit (z. B. Bootstrap läuft) | Retry mit Backoff, Status degradiert anzeigen |

## Beispielclient (passend zum FE.Library-Techstack: httpx, typisiert)

Kopierbare Vorlage für ein späteres `app/metadata_search.py` in FE.Library:

```python
"""Client for the local aa-metadata-worker search API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import httpx


@dataclass
class MetadataRecord:
    md5: str
    title: str
    authors: list[str] = field(default_factory=list)
    publisher: Optional[str] = None
    publication_year: Optional[int] = None
    languages: list[str] = field(default_factory=list)
    format: Optional[str] = None
    filesize: Optional[int] = None
    isbn13: list[str] = field(default_factory=list)
    work_key: Optional[str] = None


class MetadataApiClient:
    """Small typed wrapper around /api/v1. Degrades gracefully when offline."""

    def __init__(self, base_url: Optional[str] = None, timeout_s: float = 6.0):
        self._base_url = (base_url or os.environ.get("METADATA_API_URL", "")).rstrip("/")
        key = os.environ.get("METADATA_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(base_url=self._base_url, headers=headers, timeout=timeout_s)

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def search(
        self,
        q: Optional[str] = None,
        *,
        author: Optional[str] = None,
        isbn: Optional[str] = None,
        language: Optional[str] = None,
        extension: Optional[str] = None,
        year_from: Optional[int] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict:
        params: dict = {"limit": limit}
        if q:
            params["q"] = q
        if author:
            params["author"] = author
        if isbn:
            params["isbn"] = isbn
        if language:
            # FE.Library ISO-639-3 values are accepted directly.
            params["language"] = language
        if extension:
            params["extension"] = extension
        if year_from:
            params["year_from"] = year_from
        if cursor:
            params["cursor"] = cursor
        response = self._client.get("/api/v1/search", params=params)
        response.raise_for_status()
        return response.json()

    def record(self, md5_hex: str) -> Optional[MetadataRecord]:
        response = self._client.get(f"/api/v1/records/{md5_hex.lower()}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return MetadataRecord(
            md5=body["md5"],
            title=body["title"],
            authors=body["authors"],
            publisher=body["publisher"],
            publication_year=body["publicationYear"],
            languages=body["languages"],
            format=body["format"],
            filesize=body["filesize"],
            isbn13=body["identifiers"]["isbn13"],
            work_key=body["workKey"],
        )

    def close(self) -> None:
        self._client.close()
```

## Änderungsbedarf in FE.Library (DOKUMENTIERT, NICHT DURCHGEFÜHRT)

1. **Neues Modul** `app/metadata_search.py` mit dem Client oben (oder äquivalent).
2. **Env-Variablen** `METADATA_API_URL`, optional `METADATA_API_KEY`,
   `METADATA_API_TIMEOUT` in deren Environment-Konfiguration (`app/config.py`)
   ergänzen.
3. **Admin-Integration** analog „Buchquellen“: z. B. Panel unter
   Verbindungen → neuer Eintrag „Lokaler Metadaten-Index“ (URL + Key, Test-
   Button gegen `/api/v1/status`). Mutations sollten wie üblich audit-logged
   werden (`BOOK_SEARCH_SOURCES_UPDATED`-Muster).
4. **Catalog-Suche erweitern**: In der Admin-Buchsuche/Enrichment zusätzlich den
   lokalen Index abfragen und Treffer mit `workKey`/ISBN gegen die eigene DB
   matchen; Downloads bleiben Aufgabe der bestehenden FE.Library-Flows
   (Quellen-Hinweis via `/records/{md5}/sources`, falls gewünscht).
5. **Deploy-Compose**: `docker_bridge`-Netz für den fe_library-Container
   ergänzen (siehe oben), falls Container-zu-Container gewünscht ist.

Punkte 3–5 sind Produktentscheidungen in FE.Library und wurden hier bewusst
nicht vorweggenommen.
