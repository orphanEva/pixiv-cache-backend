from __future__ import annotations

import hashlib
import math
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.security import cache_read_allowed
from app.models.work import (
    CurrentFile,
    PixivAsset,
    PixivVersion,
    PixivWork,
    Series,
    SyncSource,
    Tag,
    WorkSource,
    WorkStatus,
    WorkTagRelation,
    WorkType,
)
from app.services.cache_service import utc_naive
from app.schemas.library import (
    AuthorList,
    AuthorSummary,
    LibraryFile,
    LibraryHistory,
    LibrarySourceRef,
    LibraryStats,
    LibraryTagRef,
    LibraryVersion,
    LibraryWorkDetail,
    LibraryWorkList,
    LibraryWorkSummary,
    PaginationMeta,
    SeriesList,
    SeriesSummary,
    TagList,
    TagSummary,
)


class LibraryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    @staticmethod
    def _pagination(page: int, page_size: int, total: int) -> PaginationMeta:
        return PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            pages=math.ceil(total / page_size) if total else 0,
        )

    async def list_works(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        work_types: list[WorkType] | None = None,
        statuses: list[WorkStatus] | None = None,
        author_id: str | None = None,
        series_id: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "all",
        source_id: int | None = None,
        is_ai: bool | None = None,
        x_restrict: int | None = None,
        cached_from=None,
        cached_to=None,
        remote_created_from=None,
        remote_created_to=None,
        search_novel_text: bool = False,
        sort: str = "cached_at",
        order: str = "desc",
    ) -> LibraryWorkList:
        conditions = []
        if q:
            predicates = [
                PixivWork.title.contains(q, autoescape=True),
                PixivWork.author_name.contains(q, autoescape=True),
                PixivWork.caption.contains(q, autoescape=True),
            ]
            if search_novel_text:
                predicates.append(
                    PixivWork.novel_content.contains(q, autoescape=True)
                )
            conditions.append(or_(*predicates))
        if work_types:
            conditions.append(PixivWork.work_type.in_(work_types))
        if statuses:
            conditions.append(PixivWork.status.in_(statuses))
        if author_id:
            conditions.append(PixivWork.author_id == author_id)
        if series_id:
            conditions.append(PixivWork.series_id == series_id)
        if is_ai is not None:
            conditions.append(PixivWork.is_ai.is_(is_ai))
        if x_restrict is not None:
            conditions.append(PixivWork.x_restrict == x_restrict)
        if cached_from is not None:
            conditions.append(PixivWork.cached_at >= utc_naive(cached_from))
        if cached_to is not None:
            conditions.append(PixivWork.cached_at <= utc_naive(cached_to))
        if remote_created_from is not None:
            conditions.append(
                PixivWork.remote_created_at >= utc_naive(remote_created_from)
            )
        if remote_created_to is not None:
            conditions.append(
                PixivWork.remote_created_at <= utc_naive(remote_created_to)
            )
        if source_id is not None:
            conditions.append(
                exists(
                    select(1).where(
                        WorkSource.work_id == PixivWork.id,
                        WorkSource.source_id == source_id,
                    )
                )
            )
        normalized_tags = [
            item.strip() for item in (tags or []) if item and item.strip()
        ]
        if normalized_tags:
            if tag_mode == "all":
                for name in normalized_tags:
                    conditions.append(
                        exists(
                            select(1)
                            .select_from(WorkTagRelation)
                            .join(Tag, Tag.id == WorkTagRelation.tag_id)
                            .where(
                                WorkTagRelation.work_id == PixivWork.id,
                                Tag.name == name,
                            )
                        )
                    )
            else:
                conditions.append(
                    exists(
                        select(1)
                        .select_from(WorkTagRelation)
                        .join(Tag, Tag.id == WorkTagRelation.tag_id)
                        .where(
                            WorkTagRelation.work_id == PixivWork.id,
                            Tag.name.in_(normalized_tags),
                        )
                    )
                )

        total = (
            await self.session.execute(
                select(func.count()).select_from(PixivWork).where(*conditions)
            )
        ).scalar_one()

        sort_map = {
            "cached_at": PixivWork.cached_at,
            "updated_at": PixivWork.updated_at,
            "remote_created_at": PixivWork.remote_created_at,
            "remote_updated_at": PixivWork.remote_updated_at,
            "title": PixivWork.title,
        }
        sort_col = sort_map.get(sort, PixivWork.cached_at)
        direction = sort_col.asc() if order == "asc" else sort_col.desc()
        stmt = (
            select(PixivWork)
            .where(*conditions)
            .options(selectinload(PixivWork.current_files))
            .order_by(sort_col.is_(None), direction, PixivWork.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        works = (await self.session.execute(stmt)).scalars().all()
        ids = [work.id for work in works]
        tag_map = await self._tag_map(ids)
        source_map = await self._source_id_map(ids)

        items = [
            self._summary(
                work,
                tags=tag_map.get(work.id, []),
                source_ids=source_map.get(work.id, []),
            )
            for work in works
        ]
        return LibraryWorkList(
            items=items,
            pagination=self._pagination(page, page_size, total),
        )

    async def get_work(
        self,
        pixiv_id: int,
        *,
        include_content: bool = False,
        include_raw_meta: bool = False,
        include_paths: bool = False,
    ) -> LibraryWorkDetail | None:
        work = (
            await self.session.execute(
                select(PixivWork)
                .where(PixivWork.id == str(pixiv_id))
                .options(selectinload(PixivWork.current_files))
            )
        ).scalar_one_or_none()
        if work is None:
            return None

        tags = (await self._tag_map([work.id])).get(work.id, [])
        sources = (await self._source_map([work.id])).get(work.id, [])
        accessible = cache_read_allowed(work.status.value)
        summary = self._summary(
            work,
            tags=tags,
            source_ids=[source.source_id for source in sources],
        )
        return LibraryWorkDetail(
            **summary.model_dump(),
            caption=work.caption,
            novel_content=(
                work.novel_content
                if include_content and accessible and work.work_type == WorkType.NOVEL
                else None
            ),
            content_available=bool(
                accessible
                and (
                    work.work_type != WorkType.NOVEL
                    or work.novel_content is not None
                )
            ),
            raw_meta=work.metadata_json if include_raw_meta else None,
            sources=sources,
            current_files=[
                self._file(
                    work,
                    current,
                    work.current_version_no,
                    accessible=accessible,
                    include_path=include_paths,
                )
                for current in sorted(
                    work.current_files,
                    key=lambda item: (item.page_index, item.file_type),
                )
            ],
        )

    async def get_history(
        self,
        pixiv_id: int,
        *,
        include_content: bool = False,
        include_raw_meta: bool = False,
        include_paths: bool = False,
    ) -> LibraryHistory | None:
        work = await self.session.get(PixivWork, str(pixiv_id))
        if work is None:
            return None
        versions = (
            await self.session.execute(
                select(PixivVersion)
                .where(PixivVersion.work_id == work.id)
                .options(selectinload(PixivVersion.assets))
                .order_by(PixivVersion.version_no.desc())
            )
        ).scalars().all()
        accessible = cache_read_allowed(work.status.value)
        return LibraryHistory(
            pixiv_id=int(work.id),
            type=work.work_type,
            current_version=work.current_version_no,
            versions=[
                self._version(
                    work,
                    version,
                    accessible=accessible,
                    include_content=include_content,
                    include_raw_meta=include_raw_meta,
                    include_paths=include_paths,
                )
                for version in versions
            ],
        )

    async def get_version(
        self,
        pixiv_id: int,
        version_no: int,
        *,
        include_content: bool = False,
        include_raw_meta: bool = False,
        include_paths: bool = False,
    ) -> LibraryVersion | None:
        work = await self.session.get(PixivWork, str(pixiv_id))
        if work is None:
            return None
        version = (
            await self.session.execute(
                select(PixivVersion)
                .where(
                    PixivVersion.work_id == work.id,
                    PixivVersion.version_no == version_no,
                )
                .options(selectinload(PixivVersion.assets))
            )
        ).scalar_one_or_none()
        if version is None:
            return None
        return self._version(
            work,
            version,
            accessible=cache_read_allowed(work.status.value),
            include_content=include_content,
            include_raw_meta=include_raw_meta,
            include_paths=include_paths,
        )

    async def verify_work(
        self,
        pixiv_id: int,
        *,
        verify_hash: bool = False,
    ) -> dict | None:
        work = (
            await self.session.execute(
                select(PixivWork)
                .where(PixivWork.id == str(pixiv_id))
                .options(selectinload(PixivWork.current_files))
            )
        ).scalar_one_or_none()
        if work is None:
            return None
        root = self.settings.storage_root.resolve()
        findings = []
        ok = True
        for item in sorted(
            work.current_files,
            key=lambda value: (value.page_index, value.file_type),
        ):
            path = Path(item.local_path).resolve()
            try:
                path.relative_to(root)
                inside_root = True
            except ValueError:
                inside_root = False
            exists_flag = inside_root and path.is_file()
            hash_ok = None
            actual_sha = None
            if verify_hash and exists_flag and item.sha256:
                actual_sha = self._sha256(path)
                hash_ok = actual_sha.lower() == item.sha256.lower()
            row_ok = inside_root and exists_flag and (hash_ok is not False)
            ok = ok and row_ok
            findings.append(
                {
                    "page_index": item.page_index,
                    "file_type": item.file_type,
                    "filename": path.name,
                    "inside_storage_root": inside_root,
                    "exists": exists_flag,
                    "hash_checked": bool(verify_hash and item.sha256),
                    "hash_ok": hash_ok,
                    "expected_sha256": item.sha256 if verify_hash else None,
                    "actual_sha256": actual_sha,
                    "size_bytes": item.size_bytes,
                }
            )
        return {
            "pixiv_id": int(work.id),
            "type": work.work_type.value,
            "version": work.current_version_no,
            "ok": ok,
            "verify_hash": verify_hash,
            "files": findings,
        }

    async def list_authors(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        sort: str = "works",
        order: str = "desc",
    ) -> AuthorList:
        conditions = [PixivWork.author_id != "0"]
        if q:
            conditions.append(
                or_(
                    PixivWork.author_name.contains(q, autoescape=True),
                    PixivWork.author_id == q,
                )
            )
        total = (
            await self.session.execute(
                select(func.count(func.distinct(PixivWork.author_id))).where(
                    *conditions
                )
            )
        ).scalar_one()

        works_count = func.count(PixivWork.id)
        latest_remote = func.max(
            func.coalesce(
                PixivWork.remote_updated_at,
                PixivWork.remote_created_at,
            )
        )
        latest_cached = func.max(PixivWork.cached_at)
        stmt = (
            select(
                PixivWork.author_id,
                func.max(PixivWork.author_name).label("author_name"),
                works_count.label("works"),
                func.sum(case((PixivWork.work_type == WorkType.ILLUST, 1), else_=0)).label("illusts"),
                func.sum(case((PixivWork.work_type == WorkType.MANGA, 1), else_=0)).label("mangas"),
                func.sum(case((PixivWork.work_type == WorkType.UGOIRA, 1), else_=0)).label("ugoira"),
                func.sum(case((PixivWork.work_type == WorkType.NOVEL, 1), else_=0)).label("novels"),
                latest_remote.label("latest_remote_at"),
                latest_cached.label("latest_cached_at"),
            )
            .where(*conditions)
            .group_by(PixivWork.author_id)
        )
        order_map = {
            "works": works_count,
            "name": func.max(PixivWork.author_name),
            "latest_remote_at": latest_remote,
            "latest_cached_at": latest_cached,
        }
        order_col = order_map.get(sort, works_count)
        stmt = stmt.order_by(
            order_col.asc() if order == "asc" else order_col.desc(),
            PixivWork.author_id.asc(),
        ).offset((page - 1) * page_size).limit(page_size)
        rows = (await self.session.execute(stmt)).all()
        return AuthorList(
            items=[
                AuthorSummary(
                    author_id=int(row.author_id),
                    author_name=row.author_name or "",
                    works=int(row.works or 0),
                    illusts=int(row.illusts or 0),
                    mangas=int(row.mangas or 0),
                    ugoira=int(row.ugoira or 0),
                    novels=int(row.novels or 0),
                    latest_remote_at=row.latest_remote_at,
                    latest_cached_at=row.latest_cached_at,
                )
                for row in rows
            ],
            pagination=self._pagination(page, page_size, total),
        )

    async def get_author(self, author_id: str) -> AuthorSummary | None:
        works_count = func.count(PixivWork.id)
        row = (
            await self.session.execute(
                select(
                    PixivWork.author_id,
                    func.max(PixivWork.author_name).label("author_name"),
                    works_count.label("works"),
                    func.sum(case((PixivWork.work_type == WorkType.ILLUST, 1), else_=0)).label("illusts"),
                    func.sum(case((PixivWork.work_type == WorkType.MANGA, 1), else_=0)).label("mangas"),
                    func.sum(case((PixivWork.work_type == WorkType.UGOIRA, 1), else_=0)).label("ugoira"),
                    func.sum(case((PixivWork.work_type == WorkType.NOVEL, 1), else_=0)).label("novels"),
                    func.max(
                        func.coalesce(
                            PixivWork.remote_updated_at,
                            PixivWork.remote_created_at,
                        )
                    ).label("latest_remote_at"),
                    func.max(PixivWork.cached_at).label("latest_cached_at"),
                )
                .where(PixivWork.author_id == author_id)
                .group_by(PixivWork.author_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return AuthorSummary(
            author_id=int(row.author_id),
            author_name=row.author_name or "",
            works=int(row.works or 0),
            illusts=int(row.illusts or 0),
            mangas=int(row.mangas or 0),
            ugoira=int(row.ugoira or 0),
            novels=int(row.novels or 0),
            latest_remote_at=row.latest_remote_at,
            latest_cached_at=row.latest_cached_at,
        )

    async def list_tags(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        sort: str = "works",
        order: str = "desc",
    ) -> TagList:
        conditions = []
        if q:
            conditions.append(
                or_(
                    Tag.name.contains(q, autoescape=True),
                    Tag.translated_name.contains(q, autoescape=True),
                )
            )
        total = (
            await self.session.execute(
                select(func.count()).select_from(Tag).where(*conditions)
            )
        ).scalar_one()
        works_count = func.count(WorkTagRelation.work_id)
        stmt = (
            select(
                Tag.id,
                Tag.name,
                Tag.translated_name,
                works_count.label("works"),
            )
            .outerjoin(
                WorkTagRelation,
                WorkTagRelation.tag_id == Tag.id,
            )
            .where(*conditions)
            .group_by(Tag.id, Tag.name, Tag.translated_name)
        )
        order_col = Tag.name if sort == "name" else works_count
        stmt = stmt.order_by(
            order_col.asc() if order == "asc" else order_col.desc(),
            Tag.id.asc(),
        ).offset((page - 1) * page_size).limit(page_size)
        rows = (await self.session.execute(stmt)).all()
        return TagList(
            items=[
                TagSummary(
                    id=row.id,
                    name=row.name,
                    translated_name=row.translated_name,
                    works=int(row.works or 0),
                )
                for row in rows
            ],
            pagination=self._pagination(page, page_size, total),
        )

    async def list_series(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        author_id: str | None = None,
        series_type: str | None = None,
        sort: str = "title",
        order: str = "asc",
    ) -> SeriesList:
        conditions = []
        if q:
            conditions.append(
                or_(
                    Series.title.contains(q, autoescape=True),
                    Series.author_name.contains(q, autoescape=True),
                )
            )
        if author_id:
            conditions.append(Series.author_id == author_id)
        if series_type:
            conditions.append(Series.type == series_type)
        total = (
            await self.session.execute(
                select(func.count()).select_from(Series).where(*conditions)
            )
        ).scalar_one()
        work_count = func.count(PixivWork.id)
        stmt = (
            select(
                Series,
                work_count.label("archived_works"),
            )
            .outerjoin(PixivWork, PixivWork.series_id == Series.id)
            .where(*conditions)
            .group_by(
                Series.id,
                Series.type,
                Series.title,
                Series.caption,
                Series.author_id,
                Series.author_name,
                Series.published_total,
                Series.is_completed,
                Series.created_at,
                Series.updated_at,
            )
        )
        order_col = work_count if sort == "works" else Series.title
        stmt = stmt.order_by(
            order_col.asc() if order == "asc" else order_col.desc(),
            Series.id.asc(),
        ).offset((page - 1) * page_size).limit(page_size)
        rows = (await self.session.execute(stmt)).all()
        return SeriesList(
            items=[
                SeriesSummary(
                    id=series.id,
                    type=series.type,
                    title=series.title,
                    caption=series.caption,
                    author_id=(
                        int(series.author_id)
                        if series.author_id and series.author_id.isdigit()
                        else None
                    ),
                    author_name=series.author_name,
                    published_total=series.published_total,
                    is_completed=series.is_completed,
                    archived_works=int(archived_works or 0),
                )
                for series, archived_works in rows
            ],
            pagination=self._pagination(page, page_size, total),
        )

    async def get_series(self, series_id: str) -> SeriesSummary | None:
        series = await self.session.get(Series, series_id)
        if series is None:
            return None
        count = (
            await self.session.execute(
                select(func.count(PixivWork.id)).where(
                    PixivWork.series_id == series_id
                )
            )
        ).scalar_one()
        return SeriesSummary(
            id=series.id,
            type=series.type,
            title=series.title,
            caption=series.caption,
            author_id=(
                int(series.author_id)
                if series.author_id and series.author_id.isdigit()
                else None
            ),
            author_name=series.author_name,
            published_total=series.published_total,
            is_completed=series.is_completed,
            archived_works=int(count or 0),
        )

    async def stats(self) -> LibraryStats:
        total = (
            await self.session.execute(select(func.count()).select_from(PixivWork))
        ).scalar_one()
        type_rows = (
            await self.session.execute(
                select(PixivWork.work_type, func.count())
                .group_by(PixivWork.work_type)
            )
        ).all()
        status_rows = (
            await self.session.execute(
                select(PixivWork.status, func.count())
                .group_by(PixivWork.status)
            )
        ).all()
        authors = (
            await self.session.execute(
                select(func.count(func.distinct(PixivWork.author_id))).where(
                    PixivWork.author_id != "0"
                )
            )
        ).scalar_one()
        tags = (
            await self.session.execute(select(func.count()).select_from(Tag))
        ).scalar_one()
        series = (
            await self.session.execute(select(func.count()).select_from(Series))
        ).scalar_one()
        versions = (
            await self.session.execute(select(func.count()).select_from(PixivVersion))
        ).scalar_one()
        current_files, current_bytes = (
            await self.session.execute(
                select(
                    func.count(CurrentFile.id),
                    func.coalesce(func.sum(CurrentFile.size_bytes), 0),
                )
            )
        ).one()
        archived_files, archive_bytes = (
            await self.session.execute(
                select(
                    func.count(PixivAsset.id),
                    func.coalesce(func.sum(PixivAsset.size_bytes), 0),
                )
            )
        ).one()
        sync_sources = (
            await self.session.execute(
                select(func.count()).select_from(SyncSource)
            )
        ).scalar_one()
        source_links = (
            await self.session.execute(
                select(func.count()).select_from(WorkSource)
            )
        ).scalar_one()
        type_counts = {value.value: 0 for value in WorkType}
        type_counts.update({row[0].value: int(row[1]) for row in type_rows})
        status_counts = {value.value: 0 for value in WorkStatus}
        status_counts.update({row[0].value: int(row[1]) for row in status_rows})
        return LibraryStats(
            works_total=int(total),
            works_by_type=type_counts,
            works_by_status=status_counts,
            authors=int(authors),
            tags=int(tags),
            series=int(series),
            versions=int(versions),
            current_files=int(current_files),
            current_storage_bytes=int(current_bytes),
            archived_files=int(archived_files),
            archive_storage_bytes=int(archive_bytes),
            sync_sources=int(sync_sources),
            source_links=int(source_links),
        )

    async def _tag_map(self, work_ids: list[str]) -> dict[str, list[LibraryTagRef]]:
        if not work_ids:
            return {}
        rows = (
            await self.session.execute(
                select(
                    WorkTagRelation.work_id,
                    Tag.name,
                    Tag.translated_name,
                )
                .join(Tag, Tag.id == WorkTagRelation.tag_id)
                .where(WorkTagRelation.work_id.in_(work_ids))
                .order_by(WorkTagRelation.work_id, Tag.name)
            )
        ).all()
        result: dict[str, list[LibraryTagRef]] = {}
        for work_id, name, translated in rows:
            result.setdefault(work_id, []).append(
                LibraryTagRef(name=name, translated_name=translated)
            )
        return result

    async def _source_id_map(self, work_ids: list[str]) -> dict[str, list[int]]:
        if not work_ids:
            return {}
        rows = (
            await self.session.execute(
                select(WorkSource.work_id, WorkSource.source_id)
                .where(WorkSource.work_id.in_(work_ids))
                .order_by(WorkSource.work_id, WorkSource.source_id)
            )
        ).all()
        result: dict[str, list[int]] = {}
        for work_id, source_id in rows:
            result.setdefault(work_id, []).append(int(source_id))
        return result

    async def _source_map(
        self,
        work_ids: list[str],
    ) -> dict[str, list[LibrarySourceRef]]:
        if not work_ids:
            return {}
        rows = (
            await self.session.execute(
                select(
                    WorkSource.work_id,
                    WorkSource.source_id,
                    SyncSource.source_type,
                    SyncSource.remote_user_id,
                    SyncSource.restrict_mode,
                    WorkSource.first_seen_at,
                    WorkSource.last_seen_at,
                )
                .join(SyncSource, SyncSource.id == WorkSource.source_id)
                .where(WorkSource.work_id.in_(work_ids))
                .order_by(WorkSource.work_id, WorkSource.source_id)
            )
        ).all()
        result: dict[str, list[LibrarySourceRef]] = {}
        for row in rows:
            result.setdefault(row.work_id, []).append(
                LibrarySourceRef(
                    source_id=int(row.source_id),
                    type=row.source_type,
                    remote_user_id=row.remote_user_id,
                    restrict=row.restrict_mode,
                    first_seen_at=row.first_seen_at,
                    last_seen_at=row.last_seen_at,
                )
            )
        return result

    def _summary(
        self,
        work: PixivWork,
        *,
        tags: list[LibraryTagRef],
        source_ids: list[int],
    ) -> LibraryWorkSummary:
        return LibraryWorkSummary(
            pixiv_id=int(work.id),
            type=work.work_type,
            status=work.status,
            accessible=cache_read_allowed(work.status.value),
            title=work.title,
            author_id=(
                int(work.author_id)
                if work.author_id and work.author_id.isdigit() and work.author_id != "0"
                else None
            ),
            author_name=work.author_name or None,
            page_count=work.page_count,
            x_restrict=work.x_restrict,
            is_ai=work.is_ai,
            series_id=work.series_id,
            series_order=work.series_order,
            remote_created_at=work.remote_created_at,
            remote_updated_at=work.remote_updated_at,
            cached_at=work.cached_at,
            updated_at=work.updated_at,
            last_checked_at=work.last_checked_at,
            current_version=work.current_version_no,
            current_file_count=len(work.current_files),
            current_size_bytes=sum(item.size_bytes for item in work.current_files),
            tags=tags,
            source_ids=source_ids,
        )

    def _file(
        self,
        work: PixivWork,
        item,
        version_no: int,
        *,
        accessible: bool,
        include_path: bool,
    ) -> LibraryFile:
        path = Path(item.local_path)
        encoded = quote(path.name)
        return LibraryFile(
            page_index=item.page_index,
            file_type=item.file_type,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            filename=path.name,
            download_url=(
                f"/media/{work.work_type.value}/{work.id}/versions/"
                f"{version_no}/{encoded}"
                if accessible
                else None
            ),
            local_path=str(path) if include_path else None,
        )

    def _version(
        self,
        work: PixivWork,
        version: PixivVersion,
        *,
        accessible: bool,
        include_content: bool,
        include_raw_meta: bool,
        include_paths: bool,
    ) -> LibraryVersion:
        return LibraryVersion(
            version=version.version_no,
            version_token=version.version_token,
            title=version.title,
            caption=version.caption,
            remote_updated_at=version.remote_updated_at,
            archived_at=version.created_at,
            tags=version.tags_snapshot or [],
            novel_content=(
                version.novel_content
                if include_content and accessible and work.work_type == WorkType.NOVEL
                else None
            ),
            raw_meta=version.metadata_json if include_raw_meta else None,
            files=[
                self._file(
                    work,
                    asset,
                    version.version_no,
                    accessible=accessible,
                    include_path=include_paths,
                )
                for asset in sorted(
                    version.assets,
                    key=lambda item: (item.page_index, item.file_type),
                )
            ],
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fp:
            while chunk := fp.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
