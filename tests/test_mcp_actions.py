from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.action_tools import register_action_tools


JOB_ID = "1234567890abcdef1234567890abcdef"
READ_TOOLS = {
    "series_archive_status", "list_jobs", "get_job", "list_sync_sources", "get_sync_source",
    "account_identity", "list_account_migrations", "get_account_migration",
    "list_account_migration_items", "ops_status",
}
WRITE_TOOLS = {
    "archive_work", "archive_series", "retry_job", "upsert_author_source", "upsert_bookmark_source",
    "upsert_series_source", "upsert_followed_series_source", "patch_sync_source", "run_sync_source",
    "create_account_migration", "retry_account_migration", "generate_ugoira_preview",
}


class RecordingBackend:
    def __init__(self):
        self.calls = []
        self.error = None
        self.response = None

    async def request(self, method, path, *, params=None, json=None) -> dict[str, Any]:
        call = {"method": method, "path": path, "params": params, "json": json}
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        response = {"ok": True, "request": call}
        if path == "/api/sync/sources":
            response["sources"] = []
        elif path.startswith("/api/jobs/series/"):
            response["works"] = []
        return response


def make_server(*, read_only=False):
    server = MCPServer("Action tool protocol tests")
    backend = RecordingBackend()
    register_action_tools(server, backend, read_only=read_only)
    return server, backend


@pytest.mark.asyncio
async def test_action_catalog_and_annotations_describe_remote_side_effects():
    server, _ = make_server()
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.idempotent_hint is False
    assert tools["account_identity"].annotations.open_world_hint is True
    assert tools["generate_ugoira_preview"].annotations.open_world_hint is False
    assert tools["patch_sync_source"].annotations.destructive_hint is True
    assert tools["archive_work"].input_schema["properties"]["expand_series"]["default"] is True
    assert tools["archive_work"].input_schema["properties"]["bookmark_if_missing"]["default"] is True
    assert tools["upsert_bookmark_source"].input_schema["properties"]["expand_series"]["default"] is False
    assert "privately bookmark" in tools["create_account_migration"].description
    for tool in tools.values():
        assert not {"url", "path", "token", "cookie", "api_key", "password", "sql"}.intersection(
            tool.input_schema["properties"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,method,path,params,body", [
    ("archive_work", {"kind": "novel", "pixiv_id": 12}, "POST", "/api/jobs/archive/novel/12",
     {"force_refresh": True, "expand_series": True, "bookmark_if_missing": True}, None),
    ("archive_work", {"kind": "ugoira", "pixiv_id": 12, "force_refresh": False,
                      "expand_series": False, "bookmark_if_missing": False},
     "POST", "/api/jobs/archive/ugoira/12",
     {"force_refresh": False, "expand_series": False, "bookmark_if_missing": False}, None),
    ("archive_series", {"series_type": "manga", "series_id": 13}, "POST", "/api/jobs/archive/series/manga/13",
     {"force_refresh": True, "bookmark_if_missing": True}, None),
    ("series_archive_status", {"series_type": "novel", "series_id": 13}, "GET", "/api/jobs/series/novel/13", None, None),
    ("list_jobs", {"queue": "media", "status": "dead", "page": 2, "page_size": 100}, "GET", "/api/media-jobs",
     {"status": "dead", "page": 2, "page_size": 100}, None),
    ("list_jobs", {}, "GET", "/api/jobs", {"status": None, "page": 1, "page_size": 50}, None),
    ("list_sync_sources", {}, "GET", "/api/sync/sources", None, None),
    ("get_sync_source", {"source_id": 14}, "GET", "/api/sync/sources/14", None, None),
    ("upsert_author_source", {"user_id": "123", "include_illust": False, "run_now": False},
     "POST", "/api/sync/sources/author/123",
     {"include_illust": False, "include_novel": True, "interval_seconds": 21600, "run_now": False}, None),
    ("upsert_bookmark_source", {}, "POST", "/api/sync/sources/bookmarks/self",
     {"restrict": "public", "include_illust": True, "include_novel": True,
      "expand_series": False, "interval_seconds": 21600, "run_now": True}, None),
    ("upsert_bookmark_source", {"user_id": "self", "restrict": "private", "expand_series": True},
     "POST", "/api/sync/sources/bookmarks/self",
     {"restrict": "private", "include_illust": True, "include_novel": True,
      "expand_series": True, "interval_seconds": 21600, "run_now": True}, None),
    ("upsert_series_source", {"series_type": "novel", "series_id": 15, "interval_seconds": 300},
     "POST", "/api/sync/sources/series/novel/15", {"interval_seconds": 300, "run_now": True}, None),
    ("upsert_followed_series_source", {"include_manga": False, "run_now": False},
     "POST", "/api/sync/sources/followed-series",
     {"include_novel": True, "include_manga": False, "interval_seconds": 21600, "run_now": False}, None),
    ("patch_sync_source", {"source_id": 16, "enabled": False, "expand_series": False},
     "PATCH", "/api/sync/sources/16", None, {"enabled": False, "expand_series": False}),
    ("patch_sync_source", {"source_id": 16, "enabled": True, "include_novel": None, "interval_seconds": 900},
     "PATCH", "/api/sync/sources/16", None, {"enabled": True, "interval_seconds": 900}),
    ("run_sync_source", {"source_id": 17, "full": True}, "POST", "/api/sync/sources/17/run", {"full": True}, None),
    ("account_identity", {}, "GET", "/api/account/identity", None, None),
    ("create_account_migration", {}, "POST", "/api/account/migrations", None, None),
    ("list_account_migrations", {"limit": 5, "before_id": 20}, "GET", "/api/account/migrations",
     {"limit": 5, "before_id": 20}, None),
    ("get_account_migration", {"run_id": 21}, "GET", "/api/account/migrations/21", None, None),
    ("list_account_migration_items", {"run_id": 22, "status": "failed", "limit": 10, "offset": 20},
     "GET", "/api/account/migrations/22/items", {"status": "failed", "limit": 10, "offset": 20}, None),
    ("retry_account_migration", {"run_id": 23}, "POST", "/api/account/migrations/23/retry", None, None),
    ("generate_ugoira_preview", {"pixiv_id": 24}, "POST", "/api/media-jobs/ugoira/24", {"version_no": None}, None),
    ("generate_ugoira_preview", {"pixiv_id": 24, "version_no": 2}, "POST", "/api/media-jobs/ugoira/24", {"version_no": 2}, None),
    ("ops_status", {}, "GET", "/api/ops/status", None, None),
    ("ops_status", {"view": "remote_rechecks"}, "GET", "/api/ops/remote-rechecks", None, None),
    ("ops_status", {"view": "media_queue"}, "GET", "/api/media-jobs/stats", None, None),
])
async def test_action_protocol_dispatch_preserves_api_parameters(name, arguments, method, path, params, body):
    server, backend = make_server()
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    expected = {"method": method, "path": path, "params": params, "json": body}
    assert backend.calls == [expected]
    if name in {"list_sync_sources", "series_archive_status"}:
        key = "sources" if name == "list_sync_sources" else "works"
        assert result.structured_content["ok"] is True
        assert result.structured_content["request"] == expected
        assert result.structured_content[key] == []
        assert result.structured_content["pagination"] == {"page": 1, "page_size": 50, "total": 0, "pages": 0}
        assert "adapter" in result.structured_content["pagination_scope"]
    else:
        assert result.structured_content == {"ok": True, "request": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("queue,prefix", [("archive", "/api/jobs"), ("sync", "/api/sync/jobs"), ("media", "/api/media-jobs")])
async def test_job_query_and_retry_select_the_right_queue(queue, prefix):
    server, backend = make_server()
    async with Client(server) as client:
        result = await client.call_tool("get_job", {"job_id": JOB_ID, "queue": queue})
        assert not result.is_error
        result = await client.call_tool("retry_job", {"job_id": JOB_ID, "queue": queue})
        assert not result.is_error
    assert [(call["method"], call["path"]) for call in backend.calls] == [
        ("GET", f"{prefix}/{JOB_ID}"), ("POST", f"{prefix}/{JOB_ID}/retry"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("archive_work", {"kind": "illust", "pixiv_id": 0}),
    ("archive_work", {"kind": "novel", "pixiv_id": -1}),
    ("archive_work", {"kind": "illust", "pixiv_id": True}),
    ("archive_work", {"kind": "illust", "pixiv_id": "12"}),
    ("archive_work", {"kind": "../ops", "pixiv_id": 12}),
    ("archive_work", {"kind": "illust", "pixiv_id": 12, "bookmark_if_missing": "false"}),
    ("archive_series", {"series_type": "ugoira", "series_id": 13}),
    ("get_job", {"job_id": "../ops/status"}),
    ("get_job", {"job_id": "https://example.invalid/private"}),
    ("get_job", {"job_id": JOB_ID, "queue": "../account"}),
    ("get_job", {"job_id": JOB_ID + "\n"}),
    ("list_jobs", {"queue": "sync"}),
    ("list_jobs", {"page_size": 201}),
    ("list_jobs", {"page": 0}),
    ("list_jobs", {"page": 1000001}),
    ("list_sync_sources", {"page_size": 101}),
    ("list_sync_sources", {"page": 1000001}),
    ("series_archive_status", {"series_type": "novel", "series_id": 13, "page_size": 101}),
    ("upsert_author_source", {"user_id": "../account"}),
    ("upsert_author_source", {"user_id": "self?run_now=false"}),
    ("upsert_author_source", {"user_id": "0"}),
    ("upsert_author_source", {"user_id": "１２"}),
    ("upsert_author_source", {"user_id": "12", "interval_seconds": 299}),
    ("upsert_author_source", {"user_id": "12", "interval_seconds": 31536001}),
    ("upsert_author_source", {"user_id": "12", "include_illust": False, "include_novel": False}),
    ("upsert_followed_series_source", {"include_novel": False, "include_manga": False}),
    ("patch_sync_source", {"source_id": 1}),
    ("patch_sync_source", {"source_id": 1, "include_illust": False, "include_novel": False}),
    ("patch_sync_source", {"source_id": 1, "interval_seconds": 31536001}),
    ("get_sync_source", {"source_id": False}),
    ("run_sync_source", {"source_id": "1/../../ops"}),
    ("list_account_migrations", {"limit": 101}),
    ("list_account_migration_items", {"run_id": 1, "offset": -1}),
    ("list_account_migration_items", {"run_id": 1, "status": "dead"}),
    ("generate_ugoira_preview", {"pixiv_id": 24, "version_no": 0}),
    ("ops_status", {"view": "https://example.invalid"}),
])
async def test_invalid_or_unsafe_arguments_never_reach_backend(name, arguments):
    server, backend = make_server()
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert backend.calls == []


@pytest.mark.asyncio
async def test_read_only_registration_removes_all_write_capabilities():
    server, backend = make_server(read_only=True)
    async with Client(server) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == READ_TOOLS
        result = await client.call_tool("archive_work", {"kind": "novel", "pixiv_id": 12})
        assert result.is_error and backend.calls == []
        result = await client.call_tool("account_identity", {})
        assert not result.is_error
    assert backend.calls == [{"method": "GET", "path": "/api/account/identity", "params": None, "json": None}]


@pytest.mark.asyncio
async def test_backend_tool_error_is_a_protocol_error_result():
    server, backend = make_server()
    backend.error = ToolError("HTTP 503: Backend maintenance; check job status before retrying")
    async with Client(server) as client:
        result = await client.call_tool("run_sync_source", {"source_id": 1})
    assert result.is_error and result.structured_content is None
    assert any("HTTP 503" in getattr(block, "text", "") for block in result.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,key,path", [
    ("list_sync_sources", {}, "sources", "/api/sync/sources"),
    ("series_archive_status", {"series_type": "novel", "series_id": 13}, "works", "/api/jobs/series/novel/13"),
])
async def test_adapter_pagination_keeps_whole_collection_counts(name, arguments, key, path):
    server, backend = make_server()
    backend.response = {
        key: [{"id": number} for number in range(1, 6)],
        "discovered": 5, "published_total": 7, "status_counts": {"archived": 3, "queued": 2},
    }
    async with Client(server) as client:
        for page, expected_ids in ((2, [3, 4]), (3, [5]), (4, [])):
            result = await client.call_tool(name, {**arguments, "page": page, "page_size": 2})
            assert not result.is_error, result.content
            data = result.structured_content
            assert [item["id"] for item in data[key]] == expected_ids
            assert data["pagination"] == {"page": page, "page_size": 2, "total": 5, "pages": 3}
            assert data["discovered"] == 5 and data["published_total"] == 7
            assert data["status_counts"] == {"archived": 3, "queued": 2}
            assert "adapter" in data["pagination_scope"]
    assert backend.calls == [{"method": "GET", "path": path, "params": None, "json": None}] * 3
    assert len(backend.response[key]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,key", [
    ("list_sync_sources", {}, "sources"),
    ("series_archive_status", {"series_type": "novel", "series_id": 13}, "works"),
])
@pytest.mark.parametrize("shape", ["missing", "null", "object"])
async def test_adapter_pagination_rejects_missing_or_invalid_arrays(name, arguments, key, shape):
    server, backend = make_server()
    backend.response = {} if shape == "missing" else {key: None if shape == "null" else {"id": 1}}
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error and result.structured_content is None
    assert any("expected" in getattr(block, "text", "") for block in result.content)
