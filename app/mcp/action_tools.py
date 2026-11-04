"""Bounded MCP operations over the existing authenticated backend API."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp.backend import BackendClient


PositiveId = Annotated[int, Field(strict=True, ge=1, le=2**64 - 1)]
UserId = Annotated[str, Field(strict=True, pattern=r"^(?:self|[1-9][0-9]{0,31})$", max_length=32)]
JobId = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)]
Boolean = Annotated[bool, Field(strict=True)]
Interval = Annotated[int, Field(strict=True, ge=300, le=31536000)]
Page = Annotated[int, Field(strict=True, ge=1, le=1000000)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=200)]
ResponsePageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
WorkKind = Literal["illust", "manga", "ugoira", "novel"]
SeriesKind = Literal["novel", "manga"]
JobQueue = Literal["archive", "sync", "media"]
JobStatus = Literal["queued", "running", "finalizing", "retry_wait", "succeeded", "failed", "dead"]
MigrationItemStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]

_JOB_PATHS = {"archive": "/api/jobs", "sync": "/api/sync/jobs", "media": "/api/media-jobs"}
_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_REMOTE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_REMOTE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
_CONFIG_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
_LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


def _require_category(first: bool, second: bool) -> None:
    if not first and not second:
        raise ToolError("At least one work category must be included")


def _response_page(data: dict[str, Any], key: str, page: int, page_size: int) -> dict[str, Any]:
    items = data.get(key)
    if not isinstance(items, list):
        raise ToolError(f"The backend response is missing the expected {key} array")
    total = len(items)
    start = (page - 1) * page_size
    return {
        **data, key: items[start:start + page_size],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "pages": (total + page_size - 1) // page_size},
        "pagination_scope": f"adapter; the backend currently returns the complete {key} array",
    }


def register_action_tools(server: MCPServer, backend: BackendClient, *, read_only: bool = False) -> None:
    """Register fixed-route tools; read-only mode omits every mutating tool."""
    def tool(annotations: ToolAnnotations):
        def register(function):
            if read_only and not annotations.read_only_hint:
                return function
            return server.tool(annotations=annotations)(function)
        return register

    @tool(_REMOTE_WRITE)
    async def archive_work(
        kind: WorkKind,
        pixiv_id: PositiveId,
        force_refresh: Boolean = True,
        expand_series: Boolean = True,
        bookmark_if_missing: Boolean = True,
    ) -> dict[str, Any]:
        """Queue a Pixiv work archive; return its job or discovered series summary.

        Makes remote requests and writes local versions. By default, a detected
        novel/manga series is expanded into all members. Missing works may be
        privately bookmarked on Pixiv when backend auto-bookmarking is enabled;
        visibility is controlled by backend configuration. Set expand_series or
        bookmark_if_missing to false to opt out of those respective behaviors.
        """
        return await backend.request("POST", f"/api/jobs/archive/{kind}/{pixiv_id}", params={
            "force_refresh": force_refresh, "expand_series": expand_series,
            "bookmark_if_missing": bookmark_if_missing,
        })

    @tool(_REMOTE_WRITE)
    async def archive_series(
        series_type: SeriesKind,
        series_id: PositiveId,
        force_refresh: Boolean = True,
        bookmark_if_missing: Boolean = True,
    ) -> dict[str, Any]:
        """Discover and queue every member of a novel or manga series.

        Makes Pixiv requests and writes series/member records and archive jobs.
        Missing members may be privately bookmarked if backend auto-bookmarking
        is enabled; privacy follows backend settings. This queues downloads,
        rather than waiting for the entire series to finish.
        """
        return await backend.request("POST", f"/api/jobs/archive/series/{series_type}/{series_id}", params={
            "force_refresh": force_refresh, "bookmark_if_missing": bookmark_if_missing,
        })

    @tool(_READ)
    async def series_archive_status(
        series_type: SeriesKind,
        series_id: PositiveId,
        page: Page = 1,
        page_size: ResponsePageSize = 50,
    ) -> dict[str, Any]:
        """Read local series archive counts and one page of member progress.

        At most 100 members are returned. Pagination happens in this adapter;
        the backend still reads and returns the entire series on each request.
        Series totals and status counts describe the complete series.
        """
        data = await backend.request("GET", f"/api/jobs/series/{series_type}/{series_id}")
        return _response_page(data, "works", page, page_size)

    @tool(_READ)
    async def list_jobs(
        queue: Literal["archive", "media"] = "archive",
        status: JobStatus | None = None,
        page: Page = 1,
        page_size: PageSize = 50,
    ) -> dict[str, Any]:
        """List archive or local MP4-generation jobs, including failed/dead jobs.

        Sync jobs have no list endpoint; use get_job with a known sync job ID.
        """
        return await backend.request("GET", _JOB_PATHS[queue], params={
            "status": status, "page": page, "page_size": page_size,
        })

    @tool(_READ)
    async def get_job(job_id: JobId, queue: JobQueue = "archive") -> dict[str, Any]:
        """Read a job by the 32-character lowercase hexadecimal ID returned by its queue."""
        return await backend.request("GET", f"{_JOB_PATHS[queue]}/{job_id}")

    @tool(_REMOTE_WRITE)
    async def retry_job(job_id: JobId, queue: JobQueue = "archive") -> dict[str, Any]:
        """Retry a terminal archive, sync or MP4 job using its existing options.

        Archive/sync retries can make Pixiv requests; archive jobs retain their
        bookmark intent and may privately bookmark missing works. Media retries
        only rebuild MP4 from local ZIP/frame files. This changes queue state.
        """
        return await backend.request("POST", f"{_JOB_PATHS[queue]}/{job_id}/retry")

    @tool(_READ)
    async def list_sync_sources(page: Page = 1, page_size: ResponsePageSize = 50) -> dict[str, Any]:
        """Read a page of persistent author, bookmark and series subscriptions.

        At most 100 sources are returned. Pagination happens in this adapter;
        the backend still reads and returns every source on each request.
        """
        data = await backend.request("GET", "/api/sync/sources")
        return _response_page(data, "sources", page, page_size)

    @tool(_READ)
    async def get_sync_source(source_id: PositiveId) -> dict[str, Any]:
        """Read one subscription, including its saved scan progress and account binding."""
        return await backend.request("GET", f"/api/sync/sources/{source_id}")

    @tool(_CONFIG_WRITE)
    async def upsert_author_source(
        user_id: UserId,
        include_illust: Boolean = True,
        include_novel: Boolean = True,
        interval_seconds: Interval = 21600,
        run_now: Boolean = True,
    ) -> dict[str, Any]:
        """Create or update an author's continuous archive subscription.

        user_id is a positive numeric string or self. Updates re-enable an
        existing source. Content-selection changes schedule a full rescan even
        if run_now is false. Workers request Pixiv and archive works locally;
        automatic source scans do not bookmark the author's works on Pixiv.
        """
        _require_category(include_illust, include_novel)
        return await backend.request("POST", f"/api/sync/sources/author/{user_id}", params={
            "include_illust": include_illust, "include_novel": include_novel,
            "interval_seconds": interval_seconds, "run_now": run_now,
        })

    @tool(_CONFIG_WRITE)
    async def upsert_bookmark_source(
        user_id: UserId = "self",
        restrict: Literal["public", "private"] = "public",
        include_illust: Boolean = True,
        include_novel: Boolean = True,
        expand_series: Boolean = False,
        interval_seconds: Interval = 21600,
        run_now: Boolean = True,
    ) -> dict[str, Any]:
        """Subscribe to a user's Pixiv bookmarks; private bookmarks require access.

        restrict selects the bookmark list to read, not the privacy of new
        bookmarks. Scans request Pixiv and archive locally without adding remote
        bookmarks. expand_series also archives and subscribes to each discovered
        collection. Updates re-enable the source; changed selections schedule a
        full rescan even when run_now is false.
        """
        _require_category(include_illust, include_novel)
        return await backend.request("POST", f"/api/sync/sources/bookmarks/{user_id}", params={
            "restrict": restrict, "include_illust": include_illust, "include_novel": include_novel,
            "expand_series": expand_series, "interval_seconds": interval_seconds, "run_now": run_now,
        })

    @tool(_CONFIG_WRITE)
    async def upsert_series_source(
        series_type: SeriesKind,
        series_id: PositiveId,
        interval_seconds: Interval = 21600,
        run_now: Boolean = True,
    ) -> dict[str, Any]:
        """Create or re-enable continuous local archiving of a novel/manga series.

        Workers request Pixiv for new members and updates. This subscription
        does not itself follow or bookmark the series on Pixiv.
        """
        return await backend.request("POST", f"/api/sync/sources/series/{series_type}/{series_id}", params={
            "interval_seconds": interval_seconds, "run_now": run_now,
        })

    @tool(_CONFIG_WRITE)
    async def upsert_followed_series_source(
        include_novel: Boolean = True,
        include_manga: Boolean = True,
        interval_seconds: Interval = 21600,
        run_now: Boolean = True,
    ) -> dict[str, Any]:
        """Import and continuously archive the configured Web account's watched series.

        Requires the backend's valid Pixiv Cookie. Scans read Pixiv watchlists
        and create local child series subscriptions; they do not add remote
        follows. Changed selections or re-enabling schedule a full rescan even
        if run_now is false. No Cookie or token is accepted by this tool.
        """
        _require_category(include_novel, include_manga)
        return await backend.request("POST", "/api/sync/sources/followed-series", params={
            "include_novel": include_novel, "include_manga": include_manga,
            "interval_seconds": interval_seconds, "run_now": run_now,
        })

    @tool(_CONFIG_WRITE)
    async def patch_sync_source(
        source_id: PositiveId,
        enabled: Boolean | None = None,
        include_illust: Boolean | None = None,
        include_novel: Boolean | None = None,
        expand_series: Boolean | None = None,
        interval_seconds: Interval | None = None,
    ) -> dict[str, Any]:
        """Change selected subscription fields; omitted fields stay unchanged.

        Disabling pauses future scans. Re-enabling or changing content selection
        schedules a full rescan when enabled, resetting saved scan progress.
        expand_series applies only to bookmark sources. Scheduled scans request
        Pixiv and write local archives without adding remote bookmarks.
        """
        data = {key: value for key, value in {
            "enabled": enabled, "include_illust": include_illust, "include_novel": include_novel,
            "expand_series": expand_series, "interval_seconds": interval_seconds,
        }.items() if value is not None}
        if not data:
            raise ToolError("Specify at least one subscription field to change")
        if include_illust is False and include_novel is False:
            raise ToolError("At least one work category must be included")
        return await backend.request("PATCH", f"/api/sync/sources/{source_id}", json=data)

    @tool(_REMOTE_WRITE)
    async def run_sync_source(source_id: PositiveId, full: Boolean = False) -> dict[str, Any]:
        """Queue an enabled source now; full requests a complete remote verification scan.

        This makes worker-side Pixiv requests and updates local archives. Saved
        progress resumes after interruptions; disabled sources must be enabled
        first. Source scans do not add remote bookmarks.
        """
        return await backend.request("POST", f"/api/sync/sources/{source_id}/run", params={"full": full})

    @tool(_REMOTE_READ)
    async def account_identity() -> dict[str, Any]:
        """Validate configured App/Web credentials against Pixiv and return account IDs.

        Performs remote authentication checks but does not archive, bookmark,
        follow or migrate anything. Credentials remain configured on the backend.
        """
        return await backend.request("GET", "/api/account/identity")

    @tool(_REMOTE_WRITE)
    async def create_account_migration() -> dict[str, Any]:
        """Queue migration of local works/series to the currently authenticated Pixiv account.

        Validates the remote account now. Workers will privately bookmark
        archived works and follow archived/subscribed series on that account.
        Existing remote bookmarks are preserved. This is an intentional remote
        write operation; no target account or credential can be supplied here.
        """
        return await backend.request("POST", "/api/account/migrations")

    @tool(_READ)
    async def list_account_migrations(
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20,
        before_id: PositiveId | None = None,
    ) -> dict[str, Any]:
        """Read migration runs newest first, using next_before_id to fetch the next page."""
        return await backend.request("GET", "/api/account/migrations", params={"limit": limit, "before_id": before_id})

    @tool(_READ)
    async def get_account_migration(run_id: PositiveId) -> dict[str, Any]:
        """Read one migration run's persisted discovery, success and failure counts."""
        return await backend.request("GET", f"/api/account/migrations/{run_id}")

    @tool(_READ)
    async def list_account_migration_items(
        run_id: PositiveId,
        status: MigrationItemStatus | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
    ) -> dict[str, Any]:
        """Read migration item results; filter failures and paginate with next_offset."""
        return await backend.request("GET", f"/api/account/migrations/{run_id}/items", params={
            "status": status, "limit": limit, "offset": offset,
        })

    @tool(_REMOTE_WRITE)
    async def retry_account_migration(run_id: PositiveId) -> dict[str, Any]:
        """Retry failed migration items belonging to the currently authenticated account.

        Revalidates Pixiv identity and schedules remote private bookmarks and
        series follows for failed items. Successful items remain recorded.
        """
        return await backend.request("POST", f"/api/account/migrations/{run_id}/retry")

    @tool(_LOCAL_WRITE)
    async def generate_ugoira_preview(pixiv_id: PositiveId, version_no: PositiveId | None = None) -> dict[str, Any]:
        """Queue MP4 reconstruction from an archived Ugoira ZIP and frame metadata.

        Uses the current version when version_no is omitted. Runs local FFmpeg
        and registers the preview; it makes no Pixiv request and preserves the
        source ZIP, frame metadata and other history versions. Poll get_job with
        queue=media for completion and the resulting media URL.
        """
        return await backend.request("POST", f"/api/media-jobs/ugoira/{pixiv_id}", params={"version_no": version_no})

    @tool(_READ)
    async def ops_status(view: Literal["status", "remote_rechecks", "media_queue"] = "status") -> dict[str, Any]:
        """Read backend/worker health, periodic remote-recheck backlog or MP4 queue counts.

        These are local operational reports; remote_rechecks reports scheduled
        work and does not trigger an immediate Pixiv check.
        """
        paths = {"status": "/api/ops/status", "remote_rechecks": "/api/ops/remote-rechecks", "media_queue": "/api/media-jobs/stats"}
        return await backend.request("GET", paths[view])
