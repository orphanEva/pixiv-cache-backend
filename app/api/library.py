from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.db.session import get_session
from app.models.work import PixivWork, WorkStatus, WorkType
from app.schemas.library import (
    AuthorList,
    AuthorSummary,
    LibraryHistory,
    LibraryStats,
    LibraryVersion,
    LibraryWorkDetail,
    LibraryWorkList,
    SeriesList,
    SeriesSummary,
    TagList,
)
from app.services.archive_jobs import ArchiveJobQueue
from app.services.library_service import LibraryService

router = APIRouter(prefix="/api/library", tags=["library"])


def service(session: AsyncSession) -> LibraryService:
    return LibraryService(session)


@router.get("/works", response_model=LibraryWorkList)
async def list_works(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, min_length=1, max_length=255),
    work_type: list[WorkType] | None = Query(None, alias="type"),
    status_filter: list[WorkStatus] | None = Query(None, alias="status"),
    author_id: str | None = Query(None),
    series_id: str | None = Query(None),
    tag: list[str] | None = Query(None),
    tag_mode: Literal["all", "any"] = Query("all"),
    source_id: int | None = Query(None, ge=1),
    is_ai: bool | None = Query(None),
    x_restrict: int | None = Query(None, ge=0),
    cached_from: datetime | None = Query(None),
    cached_to: datetime | None = Query(None),
    remote_created_from: datetime | None = Query(None),
    remote_created_to: datetime | None = Query(None),
    search_novel_text: bool = Query(False),
    sort: Literal[
        "cached_at",
        "updated_at",
        "remote_created_at",
        "remote_updated_at",
        "title",
    ] = Query("cached_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    session: AsyncSession = Depends(get_session),
):
    return await service(session).list_works(
        page=page,
        page_size=page_size,
        q=q,
        work_types=work_type,
        statuses=status_filter,
        author_id=author_id,
        series_id=series_id,
        tags=tag,
        tag_mode=tag_mode,
        source_id=source_id,
        is_ai=is_ai,
        x_restrict=x_restrict,
        cached_from=cached_from,
        cached_to=cached_to,
        remote_created_from=remote_created_from,
        remote_created_to=remote_created_to,
        search_novel_text=search_novel_text,
        sort=sort,
        order=order,
    )


@router.get("/works/{pixiv_id}", response_model=LibraryWorkDetail)
async def get_work(
    pixiv_id: int,
    include_content: bool = Query(False),
    include_raw_meta: bool = Query(False),
    include_paths: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).get_work(
        pixiv_id,
        include_content=include_content,
        include_raw_meta=include_raw_meta,
        include_paths=include_paths,
    )
    if result is None:
        raise HTTPException(404, "Archived work not found")
    return result


@router.get("/works/{pixiv_id}/history", response_model=LibraryHistory)
async def get_work_history(
    pixiv_id: int,
    include_content: bool = Query(False),
    include_raw_meta: bool = Query(False),
    include_paths: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).get_history(
        pixiv_id,
        include_content=include_content,
        include_raw_meta=include_raw_meta,
        include_paths=include_paths,
    )
    if result is None:
        raise HTTPException(404, "Archived work not found")
    return result


@router.get(
    "/works/{pixiv_id}/versions/{version_no}",
    response_model=LibraryVersion,
)
async def get_work_version(
    pixiv_id: int,
    version_no: int,
    include_content: bool = Query(False),
    include_raw_meta: bool = Query(False),
    include_paths: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).get_version(
        pixiv_id,
        version_no,
        include_content=include_content,
        include_raw_meta=include_raw_meta,
        include_paths=include_paths,
    )
    if result is None:
        raise HTTPException(404, "Archived work version not found")
    return result


@router.post(
    "/works/{pixiv_id}/refresh",
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_work(
    pixiv_id: int,
    session: AsyncSession = Depends(get_session),
):
    work = await session.get(PixivWork, str(pixiv_id))
    if work is None:
        raise HTTPException(404, "Archived work not found")
    kind = WorkType.NOVEL if work.work_type == WorkType.NOVEL else WorkType.ILLUST
    return await ArchiveJobQueue(get_redis()).enqueue(
        kind,
        pixiv_id,
        force_refresh=True,
    )


@router.get("/works/{pixiv_id}/integrity")
async def verify_work_integrity(
    pixiv_id: int,
    verify_hash: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).verify_work(
        pixiv_id,
        verify_hash=verify_hash,
    )
    if result is None:
        raise HTTPException(404, "Archived work not found")
    return result


@router.get("/authors", response_model=AuthorList)
async def list_authors(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, min_length=1, max_length=255),
    sort: Literal[
        "works", "name", "latest_remote_at", "latest_cached_at"
    ] = Query("works"),
    order: Literal["asc", "desc"] = Query("desc"),
    session: AsyncSession = Depends(get_session),
):
    return await service(session).list_authors(
        page=page,
        page_size=page_size,
        q=q,
        sort=sort,
        order=order,
    )


@router.get("/authors/{author_id}", response_model=AuthorSummary)
async def get_author(
    author_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).get_author(author_id)
    if result is None:
        raise HTTPException(404, "Archived author not found")
    return result


@router.get("/tags", response_model=TagList)
async def list_tags(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    q: str | None = Query(None, min_length=1, max_length=128),
    sort: Literal["works", "name"] = Query("works"),
    order: Literal["asc", "desc"] = Query("desc"),
    session: AsyncSession = Depends(get_session),
):
    return await service(session).list_tags(
        page=page,
        page_size=page_size,
        q=q,
        sort=sort,
        order=order,
    )


@router.get("/series", response_model=SeriesList)
async def list_series(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, min_length=1, max_length=255),
    author_id: str | None = Query(None),
    series_type: Literal["novel", "manga"] | None = Query(None, alias="type"),
    sort: Literal["title", "works"] = Query("title"),
    order: Literal["asc", "desc"] = Query("asc"),
    session: AsyncSession = Depends(get_session),
):
    return await service(session).list_series(
        page=page,
        page_size=page_size,
        q=q,
        author_id=author_id,
        series_type=series_type,
        sort=sort,
        order=order,
    )


@router.get("/series/{series_id}", response_model=SeriesSummary)
async def get_series(
    series_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await service(session).get_series(series_id)
    if result is None:
        raise HTTPException(404, "Archived series not found")
    return result


@router.get("/stats", response_model=LibraryStats)
async def library_stats(session: AsyncSession = Depends(get_session)):
    return await service(session).stats()
