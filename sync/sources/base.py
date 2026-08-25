"""Source adapter contract.

Each AA collection gets its own adapter implementing `parse`. Adapters are pure
functions from one JSONL line (dict) to zero or more NormalizedRecords; no
source-specific branching lives in the import pipeline.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from common.records import NormalizedRecord

_AACID_TS_RE = re.compile(r"__(\d{8}T\d{6}Z)__")


def aacid_timestamp(aacid: str | None) -> datetime | None:
    """Extract the scrape timestamp embedded in an AACID string."""
    if not aacid:
        return None
    match = _AACID_TS_RE.search(aacid)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class SourceAdapter(ABC):
    """Parses raw AAC records of exactly one collection."""

    collection: str

    @abstractmethod
    def parse(self, raw: dict) -> list[NormalizedRecord]:
        """Return 0..n normalized records for one JSONL line."""

    def record_timestamp(self, raw: dict) -> datetime | None:
        return aacid_timestamp(raw.get("aacid"))
