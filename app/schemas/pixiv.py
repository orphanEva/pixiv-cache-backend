from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.work import WorkStatus, WorkType


class RemoteAsset(BaseModel):
    page_index: int = 0
    url: str
    filename: str


class RemoteSnapshot(BaseModel):
    pixiv_id: int
    work_type: WorkType
    title: str = ""
    caption: str | None = None
    tags: list[dict] = Field(default_factory=list)
    x_restrict: int = 0
    is_ai: bool = False
    series_id: str | None = None
    series_order: int | None = None
    author_id: int | None = None
    author_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)
    assets: list[RemoteAsset] = Field(default_factory=list)
    text_content: str | None = None
    version_token: str


class AssetResponse(BaseModel):
    page_index: int
    local_path: str
    sha256: str
    size_bytes: int


class VersionSummary(BaseModel):
    version: int
    version_token: str
    remote_updated_at: datetime | None
    created_at: datetime
    assets: list[AssetResponse]


class WorkResponse(BaseModel):
    pixiv_id: int
    type: WorkType
    status: WorkStatus
    status_reason: str | None = None
    title: str
    author_id: int | None
    author_name: str | None
    remote_created_at: datetime | None
    remote_updated_at: datetime | None
    version: int
    version_token: str
    cached_at: datetime
    last_checked_at: datetime
    source: str
    assets: list[AssetResponse]
    novel_text_path: str | None = None


class HistoryResponse(BaseModel):
    pixiv_id: int
    type: WorkType
    current_version: int
    versions: list[VersionSummary]
