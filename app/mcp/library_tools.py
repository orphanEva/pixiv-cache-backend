"""Bounded, read-only MCP access to the backend's local library API."""
from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

if TYPE_CHECKING:
    from app.mcp.backend import BackendClient

Kind = Literal["illust", "novel"]
SeriesKind = Literal["novel", "manga"]
Status = Literal["active", "deleted", "private", "unavailable", "auth_required", "error"]
Order = Literal["asc", "desc"]
PositiveID = Annotated[int, Field(gt=0, le=9223372036854775807, strict=True)]
Page = Annotated[int, Field(ge=1, le=1000000, strict=True)]
PageSize = Annotated[int, Field(ge=1, le=50, strict=True)]
Offset = Annotated[int, Field(ge=0, le=1000000000, strict=True)]
FileLimit = Annotated[int, Field(ge=1, le=100, strict=True)]
QueryText = Annotated[str, Field(min_length=1, max_length=255)]
TagText = Annotated[str, Field(min_length=1, max_length=128)]
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_PRIVATE_FIELDS = {
    "raw_meta", "metadata_json", "novel_content", "storage_root", "path", "paths",
    "cookie", "cookies", "credentials", "authorization", "password", "secret",
    "client_secret", "api_key", "token", "remote_url", "headers", "env",
    "apikey", "clientsecret", "refreshtoken", "accesstoken", "set_cookie", "setcookie",
    "database_url", "redis_url",
}


def _clean(value: Any) -> Any:
    """Discard internal paths/credentials even if an older backend returns them.

    Metadata strings and nested lists are bounded. Novel text is handled by its
    dedicated excerpt mapper so character offsets remain exact.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _PRIVATE_FIELDS or normalized.endswith(("_path", "_paths", "_token", "_cookie")):
                continue
            if normalized == "filename" and isinstance(item, str):
                item = item.replace("\\", "/").rsplit("/", 1)[-1]
            if normalized == "download_url" and item is not None:
                # Library URLs are relative media routes, never filesystem URLs.
                item = item if isinstance(item, str) and item.startswith("/media/") else None
            if isinstance(item, str) and len(item) > 4000:
                result[key] = item[:4000]
                result[f"{key}_truncated"] = True
            elif isinstance(item, list) and len(item) > 100:
                result[key] = [_clean(row) for row in item[:100]]
                result[f"{key}_total"] = len(item)
                result[f"{key}_truncated"] = True
            else:
                result[key] = _clean(item)
        return result
    if isinstance(value, list):
        return [_clean(item) for item in value[:100]]
    return value


def _items(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ToolError("The backend returned an unexpected library list")
    return value


def _file_page(data: dict, key: str, offset: int, limit: int) -> dict:
    files = _items(data, key)
    result = _clean({name: value for name, value in data.items() if name != key})
    result[key] = _clean(files[offset:offset + limit])
    result["file_pagination"] = {
        "offset": offset, "limit": limit, "total": len(files),
        "next_offset": offset + limit if offset + limit < len(files) else None,
    }
    return result


def _history_page(data: dict, page: int, page_size: int) -> dict:
    versions = _items(data, "versions")
    start = (page - 1) * page_size
    items = []
    for version in versions[start:start + page_size]:
        files = _items(version, "files")
        summary = _clean({key: value for key, value in version.items() if key != "files"})
        summary["file_count"] = len(files)
        summary["file_types"] = sorted({item.get("file_type", "unknown") for item in files})
        items.append(summary)
    return {
        **_clean({key: value for key, value in data.items() if key != "versions"}),
        "versions": items,
        "pagination": {"page": page, "page_size": page_size, "total": len(versions),
                       "pages": math.ceil(len(versions) / page_size)},
        "pagination_scope": "adapter; the backend currently returns the complete history",
    }


def _novel_excerpt(data: dict, pixiv_id: int, version_no: int | None, offset: int, length: int) -> dict:
    if data.get("type") not in (None, "novel"):
        raise ToolError("The archived work is not a novel")
    text = data.get("novel_content")
    version = data.get("version", data.get("current_version", version_no))
    result = {"pixiv_id": pixiv_id, "kind": "novel", "version": version,
              "title": str(data.get("title") or "")[:4000]}
    if text is None or data.get("accessible") is False or data.get("content_available") is False:
        return {**result, "content_available": False, "text": None, "next_offset": None,
                "reason": "Novel text is missing or not accessible in the local archive"}
    if not isinstance(text, str):
        raise ToolError("The backend returned unexpected novel content")
    start = min(offset, len(text))
    end = min(start + length, len(text))
    return {**result, "content_available": True, "offset": start, "requested_offset": offset,
            "end_offset": end, "total_characters": len(text), "text": text[start:end],
            "next_offset": end if end < len(text) else None, "has_more": end < len(text)}


def register_library_tools(server: MCPServer, backend: BackendClient) -> None:
    """Register local-only reads; no cache-fill or Pixiv refresh endpoints."""

    async def read(path: str, **params) -> dict[str, Any]:
        return await backend.request("GET", f"/api/library{path}", params=params or None)

    def detail_options(kind: Kind, *, content: bool = False) -> dict:
        return {"kind": kind, "include_content": content,
                "include_raw_meta": False, "include_paths": False}

    @server.tool(annotations=READ_ONLY)
    async def search_library(
        q: QueryText | None = None, kind: Kind | None = None,
        page: Page = 1, page_size: PageSize = 20,
        statuses: Annotated[list[Status], Field(max_length=6)] | None = None,
        author_id: PositiveID | None = None, series_id: PositiveID | None = None,
        series_type: SeriesKind | None = None,
        tags: Annotated[list[TagText], Field(max_length=20)] | None = None,
        tag_mode: Literal["all", "any"] = "all", source_id: PositiveID | None = None,
        is_ai: bool | None = None, x_restrict: Annotated[int, Field(ge=0, le=2)] | None = None,
        cached_from: datetime | None = None, cached_to: datetime | None = None,
        remote_created_from: datetime | None = None, remote_created_to: datetime | None = None,
        search_novel_text: bool = False,
        sort: Literal["cached_at", "updated_at", "remote_created_at", "remote_updated_at", "title"] = "cached_at",
        order: Order = "desc",
    ) -> dict[str, Any]:
        """Search already archived works, at most 50 per page; never download or refresh.

        kind=illust includes illustrations, manga and Ugoira. kind=novel selects
        novels. A series_id requires series_type to distinguish overlapping IDs.
        search_novel_text enables a potentially expensive local full-text scan.
        """
        if series_id is not None and series_type is None:
            raise ToolError("Specify series_type when filtering by series_id")
        for lower, upper in ((cached_from, cached_to), (remote_created_from, remote_created_to)):
            if lower is not None and upper is not None:
                try:
                    if lower > upper:
                        raise ToolError("A date range starts after it ends")
                except TypeError:
                    raise ToolError("Use matching timezone formats for both ends of each date range") from None
        types = ["novel"] if kind == "novel" else ["illust", "manga", "ugoira"] if kind == "illust" else None
        data = await read("/works", q=q, type=types, page=page, page_size=page_size,
            status=statuses, author_id=author_id, series_id=series_id, series_type=series_type,
            tag=tags, tag_mode=tag_mode, source_id=source_id, is_ai=is_ai, x_restrict=x_restrict,
            cached_from=cached_from.isoformat() if cached_from else None,
            cached_to=cached_to.isoformat() if cached_to else None,
            remote_created_from=remote_created_from.isoformat() if remote_created_from else None,
            remote_created_to=remote_created_to.isoformat() if remote_created_to else None,
            search_novel_text=search_novel_text, sort=sort, order=order)
        return _clean({**data, "items": _items(data, "items")[:page_size]})

    @server.tool(annotations=READ_ONLY)
    async def get_library_work(
        pixiv_id: PositiveID, kind: Kind, file_offset: Offset = 0, file_limit: FileLimit = 50,
    ) -> dict[str, Any]:
        """Get archived metadata and a bounded page of current media links.

        Always specify kind to distinguish identical novel/illustration IDs.
        Use read_novel_excerpt for text; this tool omits text and internal paths.
        """
        data = await read(f"/works/{pixiv_id}", **detail_options(kind))
        return {**_file_page(data, "current_files", file_offset, file_limit), "kind": kind}

    @server.tool(annotations=READ_ONLY)
    async def get_library_history(
        pixiv_id: PositiveID, kind: Kind, page: Page = 1, page_size: PageSize = 20,
    ) -> dict[str, Any]:
        """List archived version summaries, newest first, without novel text or files.

        At most 50 summaries are returned. Pagination happens in this adapter;
        the current backend still reads its full history on each request.
        Use get_library_version for a selected version's files.
        """
        data = await read(f"/works/{pixiv_id}/history", **detail_options(kind))
        return {**_history_page(data, page, page_size), "kind": kind}

    @server.tool(annotations=READ_ONLY)
    async def get_library_version(
        pixiv_id: PositiveID, kind: Kind, version_no: PositiveID,
        file_offset: Offset = 0, file_limit: FileLimit = 50,
    ) -> dict[str, Any]:
        """Get one local history version and at most 100 media links, without text or paths."""
        data = await read(f"/works/{pixiv_id}/versions/{version_no}", **detail_options(kind))
        return {**_file_page(data, "files", file_offset, file_limit), "pixiv_id": pixiv_id, "kind": kind}

    @server.tool(annotations=READ_ONLY)
    async def read_novel_excerpt(
        pixiv_id: PositiveID, version_no: PositiveID | None = None,
        offset: Offset = 0, length: Annotated[int, Field(ge=1, le=12000, strict=True)] = 4000,
    ) -> dict[str, Any]:
        """Read up to 12,000 Unicode characters from an archived novel, with next_offset.

        Offsets count characters, not bytes. Pin version_no to keep subsequent
        excerpts stable. Missing/inaccessible text returns content_available=false.
        This reads local library data only; it never downloads the novel.
        The backend currently sends the full text to this adapter before slicing.
        """
        path = f"/works/{pixiv_id}" if version_no is None else f"/works/{pixiv_id}/versions/{version_no}"
        data = await read(path, **detail_options("novel", content=True))
        return _novel_excerpt(data, pixiv_id, version_no, offset, length)

    @server.tool(annotations=READ_ONLY)
    async def check_library_integrity(
        pixiv_id: PositiveID, kind: Kind, verify_hash: bool = False,
        file_offset: Offset = 0, file_limit: FileLimit = 50,
    ) -> dict[str, Any]:
        """Check registered local files without modifying, repairing or downloading them.

        verify_hash=true reads file contents and can be slow for large archives.
        File results are paged here; the backend checks all files in this work.
        """
        data = await read(f"/works/{pixiv_id}/integrity", kind=kind, verify_hash=verify_hash)
        return {**_file_page(data, "files", file_offset, file_limit), "kind": kind}

    @server.tool(annotations=READ_ONLY)
    async def list_library_authors(
        q: QueryText | None = None, page: Page = 1, page_size: PageSize = 20,
        sort: Literal["works", "name", "latest_remote_at", "latest_cached_at"] = "works",
        order: Order = "desc",
    ) -> dict[str, Any]:
        """List/search authors represented in the local archive, at most 50 per page."""
        data = await read("/authors", q=q, page=page, page_size=page_size, sort=sort, order=order)
        return _clean({**data, "items": _items(data, "items")[:page_size]})

    @server.tool(annotations=READ_ONLY)
    async def get_library_author(author_id: PositiveID) -> dict[str, Any]:
        """Get an archived author's local work counts and latest timestamps."""
        return _clean(await read(f"/authors/{author_id}"))

    @server.tool(annotations=READ_ONLY)
    async def list_library_tags(
        q: TagText | None = None, page: Page = 1, page_size: PageSize = 20,
        sort: Literal["works", "name"] = "works", order: Order = "desc",
    ) -> dict[str, Any]:
        """List/search tags and local work counts, at most 50 tags per page."""
        data = await read("/tags", q=q, page=page, page_size=page_size, sort=sort, order=order)
        return _clean({**data, "items": _items(data, "items")[:page_size]})

    @server.tool(annotations=READ_ONLY)
    async def list_library_series(
        q: QueryText | None = None, author_id: PositiveID | None = None,
        series_type: SeriesKind | None = None, page: Page = 1, page_size: PageSize = 20,
        sort: Literal["title", "works"] = "title", order: Order = "asc",
    ) -> dict[str, Any]:
        """List/search archived novel or manga series, at most 50 per page."""
        data = await read("/series", q=q, author_id=author_id, type=series_type,
                          page=page, page_size=page_size, sort=sort, order=order)
        return _clean({**data, "items": _items(data, "items")[:page_size]})

    @server.tool(annotations=READ_ONLY)
    async def get_library_series(series_type: SeriesKind, series_id: PositiveID) -> dict[str, Any]:
        """Get one local series summary; search_library with both series fields lists its works."""
        return _clean(await read(f"/series/{series_type}/{series_id}"))

    @server.tool(annotations=READ_ONLY)
    async def get_library_stats() -> dict[str, Any]:
        """Get local work/type/status counts, authors, tags, series and storage totals."""
        return _clean(await read("/stats"))
