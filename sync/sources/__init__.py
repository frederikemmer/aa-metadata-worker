"""Adapter registry: collection name -> adapter instance."""

from __future__ import annotations

from sync.sources.base import SourceAdapter
from sync.sources.ia2 import Ia2Adapter
from sync.sources.uploads import UploadsAdapter
from sync.sources.zlib3 import Zlib3Adapter

_ADAPTERS: dict[str, SourceAdapter] = {
    adapter.collection: adapter  # type: ignore[attr-defined]
    for adapter in (Zlib3Adapter(), Ia2Adapter(), UploadsAdapter())
}


def get_adapter(collection: str) -> SourceAdapter:
    if collection not in _ADAPTERS:
        raise KeyError(f"No source adapter registered for collection '{collection}'")
    return _ADAPTERS[collection]


def known_collections() -> list[str]:
    return sorted(_ADAPTERS)
