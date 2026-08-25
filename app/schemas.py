"""Pydantic response schemas for the metadata API (contract /api/v1)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Identifiers(BaseModel):
    isbn10: list[str] = Field(default_factory=list)
    isbn13: list[str] = Field(default_factory=list)
    doi: list[str] = Field(default_factory=list)
    oclc: list[str] = Field(default_factory=list)
    openlibrary: list[str] = Field(default_factory=list)


class SourceInfo(BaseModel):
    collection: str
    record_id: str | None = None
    aacid: str | None = None


class RecordResponse(BaseModel):
    md5: str
    title: str
    authors: list[str] = Field(default_factory=list)
    publisher: str | None = None
    publicationYear: int | None = None
    languages: list[str] = Field(default_factory=list)
    format: str | None = None
    filesize: int | None = None
    identifiers: Identifiers = Field(default_factory=Identifiers)
    workKey: str | None = None
    source: SourceInfo


class SearchResponse(BaseModel):
    totalLowerBound: int
    limit: int
    nextCursor: str | None = None
    results: list[RecordResponse] = Field(default_factory=list)


class RecordSourcesResponse(BaseModel):
    md5: str
    aaPageUrl: str
    ipfsCid: str | None = None
    source: SourceInfo
    note: str = "Reference information only. This service does not host or proxy book files."


class SyncState(BaseModel):
    status: str
    activeRelease: str | None = None


class StatusResponse(BaseModel):
    ready: bool
    records: int
    lastSuccessfulSync: str | None = None
    collections: list[str]
    databaseSizeBytes: int
    diskFreeBytes: int
    schemaVersion: int
    sync: SyncState


class HealthLive(BaseModel):
    status: str = "live"


class HealthReady(BaseModel):
    ready: bool
    schemaVersion: int
    migrationsApplied: bool
    message: str = ""


class ErrorResponse(BaseModel):
    detail: str
