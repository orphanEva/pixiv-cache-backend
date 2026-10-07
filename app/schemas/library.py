from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.work import (
    SyncRestrict,
    SyncSourceType,
    WorkStatus,
    WorkType,
)


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int


class LibraryTagRef(BaseModel):
    name: str
    translated_name: str | None = None


class LibrarySourceRef(BaseModel):
    source_id: int
    type: SyncSourceType
    remote_user_id: str
    restrict: SyncRestrict
    first_seen_at: datetime
    last_seen_at: datetime


class LibraryFile(BaseModel):
    page_index: int
    file_type: str
    size_bytes: int
    sha256: str | None = None
    filename: str
    download_url: str | None = None
    local_path: str | None = None


class LibraryWorkSummary(BaseModel):
    pixiv_id: int
    type: WorkType
    status: WorkStatus
    accessible: bool
    title: str
    author_id: int | None = None
    author_name: str | None = None
    page_count: int
    x_restrict: int
    is_ai: bool
    series_id: str | None = None
    series_order: int | None = None
    remote_created_at: datetime | None = None
    remote_updated_at: datetime | None = None
    cached_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    current_version: int
    current_file_count: int
    current_size_bytes: int
    tags: list[LibraryTagRef] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)


class LibraryWorkList(BaseModel):
    items: list[LibraryWorkSummary]
    pagination: PaginationMeta


class LibraryWorkDetail(LibraryWorkSummary):
    caption: str | None = None
    novel_content: str | None = None
    content_available: bool
    raw_meta: dict[str, Any] | None = None
    sources: list[LibrarySourceRef] = Field(default_factory=list)
    current_files: list[LibraryFile] = Field(default_factory=list)


class LibraryVersion(BaseModel):
    version: int
    version_token: str
    title: str
    caption: str | None = None
    remote_updated_at: datetime | None = None
    archived_at: datetime
    tags: list[dict[str, Any]] = Field(default_factory=list)
    novel_content: str | None = None
    raw_meta: dict[str, Any] | None = None
    files: list[LibraryFile] = Field(default_factory=list)


class LibraryHistory(BaseModel):
    pixiv_id: int
    type: WorkType
    current_version: int
    versions: list[LibraryVersion]


class AuthorSummary(BaseModel):
    author_id: int
    author_name: str
    works: int
    illusts: int
    mangas: int
    ugoira: int
    novels: int
    latest_remote_at: datetime | None = None
    latest_cached_at: datetime | None = None


class AuthorList(BaseModel):
    items: list[AuthorSummary]
    pagination: PaginationMeta


class TagSummary(BaseModel):
    id: int
    name: str
    translated_name: str | None = None
    works: int


class TagList(BaseModel):
    items: list[TagSummary]
    pagination: PaginationMeta


class SeriesSummary(BaseModel):
    id: str
    type: str
    title: str
    caption: str | None = None
    author_id: int | None = None
    author_name: str
    published_total: int
    is_completed: bool
    archived_works: int


class SeriesList(BaseModel):
    items: list[SeriesSummary]
    pagination: PaginationMeta


class LibraryStats(BaseModel):
    works_total: int
    works_by_type: dict[str, int]
    works_by_status: dict[str, int]
    authors: int
    tags: int
    series: int
    versions: int
    current_files: int
    current_storage_bytes: int
    archived_files: int
    archive_storage_bytes: int
    sync_sources: int
    source_links: int
