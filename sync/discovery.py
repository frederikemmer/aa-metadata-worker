"""Release discovery against the official AA torrents.json manifest.

Endpoint (mirror configurable): GET {AA_MIRROR_BASE_URL}/dyn/torrents.json
Each entry carries display_name like
  annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent
plus btih, magnet_link, url, data_size, obsolete, is_metadata.

Selection rule per collection: among non-obsolete metadata torrents choose the
one with the newest end timestamp; cumulative range releases supersede older
ones and are byte-identical for shared ranges (verified via AAC.md).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_RELEASE_RE = re.compile(
    r"^annas_archive_meta__aacid__(?P<collection>[a-z0-9_]+?)__"
    r"(?P<from>\d{8}T\d{6}Z)--(?P<to>\d{8}T\d{6}Z)\.jsonl\.seekable\.zst$"
)


@dataclass(frozen=True)
class ReleaseInfo:
    collection: str
    identifier: str  # e.g. annas_archive_meta__aacid__zlib3_records__...--....jsonl.seekable.zst
    btih: str
    torrent_url: str
    magnet_link: str
    data_size_bytes: int


def parse_release(entry: dict) -> ReleaseInfo | None:
    name = entry.get("display_name") or ""
    if not entry.get("is_metadata") or entry.get("obsolete"):
        return None
    stem = name.removesuffix(".torrent")
    match = _RELEASE_RE.match(stem)
    if not match:
        return None
    return ReleaseInfo(
        collection=match.group("collection"),
        identifier=stem,
        btih=entry.get("btih") or "",
        torrent_url=entry.get("url") or "",
        magnet_link=entry.get("magnet_link") or "",
        data_size_bytes=int(entry.get("data_size") or 0),
    )


def fetch_manifest(base_url: str, timeout_s: float = 60.0, retries: int = 3) -> list[dict]:
    url = f"{base_url.rstrip('/')}/dyn/torrents.json"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = httpx.get(url, timeout=timeout_s, follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("torrents.json did not contain a JSON array")
            return payload
        except Exception as error:  # noqa: BLE001 - retry any transport/parse failure
            last_error = error
            logger.warning("Manifest fetch failed (attempt %d/%d): %s", attempt, retries, error)
            time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"Could not fetch torrents manifest from {url}: {last_error}")


def latest_releases(manifest: list[dict], collections: list[str]) -> dict[str, ReleaseInfo]:
    """Map collection -> newest non-obsolete release."""
    wanted = set(collections)
    best: dict[str, ReleaseInfo] = {}
    for entry in manifest:
        release = parse_release(entry)
        if release is None or release.collection not in wanted:
            continue
        current = best.get(release.collection)
        # Identifier ends with the 'to' timestamp; lexicographic order == chronological.
        if current is None or release.identifier > current.identifier:
            best[release.collection] = release
    return best


def find_release(manifest: list[dict], collection: str, identifier_suffix: str) -> ReleaseInfo:
    """Find exactly one release of `collection` whose identifier ends with the suffix.

    Operational escape hatch for bootstrapping from an older (better seeded)
    cumulative release when the newest one has no seeders yet. Raises ValueError
    with the candidate list on zero or ambiguous matches.
    """
    candidates = [
        release
        for entry in manifest
        if (release := parse_release(entry)) is not None
        and release.collection == collection
        and release.identifier.endswith(identifier_suffix)
    ]
    if not candidates:
        raise ValueError(
            f"No release found for collection '{collection}' "
            f"with identifier suffix '{identifier_suffix}'."
        )
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous identifier suffix '{identifier_suffix}' for collection '{collection}': "
            f"{len(candidates)} matches ({[c.identifier for c in candidates]})."
        )
    return candidates[0]
