from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

pytest.importorskip("mcp")

from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.library_tools import register_library_tools


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.response = {}

    async def request(self, method, path, *, params=None, json=None):
        assert method == "GET"
        assert path.startswith("/api/library/")
        self.calls.append((method, path, params, json))
        if isinstance(self.response, Exception):
            raise self.response
        return deepcopy(self.response)


@pytest.fixture
def library():
    @asynccontextmanager
    async def connect():
        backend = FakeBackend()
        server = MCPServer("library-unit-test")
        register_library_tools(server, backend)
        async with Client(server) as client:
            yield client, backend
    return connect


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert result.is_error is False, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.mark.asyncio
async def test_library_tool_contracts_are_read_only_bounded_and_namespace_explicit(library):
    async with library() as (client, _):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert len(tools) == 12
        assert {"search_library", "read_novel_excerpt", "check_library_integrity", "get_library_stats"} <= set(tools)
        for tool in tools.values():
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.open_world_hint is False
            assert "include_paths" not in tool.input_schema.get("properties", {})
            assert "include_raw_meta" not in tool.input_schema.get("properties", {})
        work = tools["get_library_work"].input_schema
        assert "kind" in work["required"]
        assert work["properties"]["kind"]["enum"] == ["illust", "novel"]
        assert tools["read_novel_excerpt"].input_schema["properties"]["length"]["maximum"] == 12000
        assert tools["search_library"].input_schema["properties"]["page_size"]["maximum"] == 50


@pytest.mark.asyncio
async def test_search_maps_illustration_namespace_and_preserves_false_zero_and_list_filters(library):
    async with library() as (client, backend):
        backend.response = {"items": [{"pixiv_id": 7, "type": "manga"}], "pagination": {"total": 1}}
        result = await call(client, "search_library", kind="illust", q="cat", page_size=10,
                            tags=["cat", "blue"], tag_mode="any", statuses=["active"],
                            is_ai=False, x_restrict=0, series_id=22, series_type="manga",
                            cached_from="2026-01-01T00:00:00Z", cached_to="2026-12-31T23:59:59Z")
        method, path, params, body = backend.calls[-1]
        assert (method, path, body) == ("GET", "/api/library/works", None)
        assert params["type"] == ["illust", "manga", "ugoira"]
        assert params["tag"] == ["cat", "blue"] and params["tag_mode"] == "any"
        assert params["status"] == ["active"] and params["is_ai"] is False and params["x_restrict"] == 0
        assert params["cached_from"] == "2026-01-01T00:00:00+00:00"
        assert result["items"][0]["type"] == "manga"


@pytest.mark.asyncio
async def test_work_files_are_paged_and_internal_metadata_is_removed(library):
    async with library() as (client, backend):
        backend.response = {
            "pixiv_id": 77, "type": "ugoira", "title": "animation", "novel_content": "hidden-body",
            "raw_meta": {"cookie": "private-cookie"}, "storage_path": "E:/private/storage",
            "tags": [{"name": "tag", "refresh_token": "private-refresh"}],
            "current_files": [{"page_index": i, "filename": "E:\\private\\frame.jpg",
                               "download_url": f"/media/ugoira/77/versions/1/{i}.jpg",
                               "local_path": "E:/private/file", "authorization": "private-auth"}
                              for i in range(150)],
        }
        result = await call(client, "get_library_work", pixiv_id=77, kind="illust", file_offset=100, file_limit=10)
        assert len(result["current_files"]) == 10
        assert result["current_files"][0]["page_index"] == 100
        assert result["current_files"][0]["filename"] == "frame.jpg"
        assert result["file_pagination"] == {"offset": 100, "limit": 10, "total": 150, "next_offset": 110}
        serialized = str(result)
        assert "private-" not in serialized and "E:/private" not in serialized and "hidden-body" not in serialized
        assert backend.calls[-1][2] == {"kind": "illust", "include_content": False,
                                       "include_raw_meta": False, "include_paths": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("get_library_work", {"pixiv_id": 1, "kind": "manga"}),
    ("get_library_work", {"pixiv_id": 0, "kind": "illust"}),
    ("get_library_work", {"pixiv_id": 1}),
    ("get_library_work", {"pixiv_id": 1, "kind": "illust", "file_limit": 101}),
    ("get_library_history", {"pixiv_id": 1, "kind": "novel", "page_size": 51}),
    ("read_novel_excerpt", {"pixiv_id": 1, "length": 12001}),
    ("read_novel_excerpt", {"pixiv_id": 1, "offset": -1}),
    ("search_library", {"series_id": 10}),
    ("search_library", {"tags": ["tag"] * 21}),
    ("search_library", {"cached_from": "2026-12-01", "cached_to": "2026-01-01"}),
])
async def test_invalid_tool_inputs_fail_before_any_backend_request(library, tool, arguments):
    async with library() as (client, backend):
        result = await client.call_tool(tool, arguments)
        assert result.is_error is True
        assert backend.calls == []


@pytest.mark.asyncio
async def test_novel_excerpt_uses_local_version_and_unicode_offsets(library):
    async with library() as (client, backend):
        body = "开🙂头" + "正文" * 8000
        backend.response = {"version": 3, "title": "story", "novel_content": body,
                            "local_path": "private-path", "raw_meta": {"access_token": "private-token"}}
        result = await call(client, "read_novel_excerpt", pixiv_id=77, version_no=3, offset=1, length=12000)
        assert result["text"] == body[1:12001]
        assert len(result["text"]) == 12000 and result["next_offset"] == 12001
        assert result["total_characters"] == len(body) and result["version"] == 3
        assert "private-" not in str(result)
        _, path, params, _ = backend.calls[-1]
        assert path == "/api/library/works/77/versions/3"
        assert params["kind"] == "novel" and params["include_content"] is True
        ending = await call(client, "read_novel_excerpt", pixiv_id=77, version_no=3, offset=result["next_offset"], length=12000)
        assert ending["text"] == body[12001:]
        assert ending["next_offset"] is None and ending["has_more"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("response,available", [
    ({"novel_content": None, "content_available": False}, False),
    ({"novel_content": "do-not-expose", "accessible": False}, False),
    ({"novel_content": "", "content_available": True}, True),
])
async def test_missing_or_inaccessible_novel_does_not_look_like_available_text(library, response, available):
    async with library() as (client, backend):
        backend.response = response
        result = await call(client, "read_novel_excerpt", pixiv_id=1)
        assert result["content_available"] is available
        assert result["text"] == ("" if available else None)
        assert result["next_offset"] is None


@pytest.mark.asyncio
async def test_history_is_summary_only_with_explicit_adapter_pagination(library):
    async with library() as (client, backend):
        backend.response = {"pixiv_id": 1, "current_version": 7, "type": "novel",
            "versions": [{"version": number, "title": f"v{number}", "novel_content": "hidden",
                          "files": [{"file_type": "novel_txt", "local_path": "private"}]} for number in range(7, 0, -1)]}
        result = await call(client, "get_library_history", pixiv_id=1, kind="novel", page=2, page_size=3)
        assert [version["version"] for version in result["versions"]] == [4, 3, 2]
        assert result["pagination"] == {"page": 2, "page_size": 3, "total": 7, "pages": 3}
        assert "adapter" in result["pagination_scope"]
        assert all("files" not in row and "novel_content" not in row for row in result["versions"])
        assert result["versions"][0]["file_count"] == 1
        assert backend.calls[-1][2]["include_content"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,path", [
    ("get_library_version", {"pixiv_id": 7, "kind": "novel", "version_no": 2}, "/works/7/versions/2"),
    ("check_library_integrity", {"pixiv_id": 7, "kind": "illust", "verify_hash": True}, "/works/7/integrity"),
    ("list_library_authors", {}, "/authors"),
    ("get_library_author", {"author_id": 12}, "/authors/12"),
    ("list_library_tags", {"q": "猫"}, "/tags"),
    ("list_library_series", {"series_type": "manga"}, "/series"),
    ("get_library_series", {"series_type": "novel", "series_id": 12}, "/series/novel/12"),
    ("get_library_stats", {}, "/stats"),
])
async def test_remaining_tools_use_only_read_only_library_routes(library, tool, arguments, path):
    async with library() as (client, backend):
        backend.response = {"items": [], "files": [], "works_total": 3}
        await call(client, tool, **arguments)
        assert backend.calls[-1][0] == "GET"
        assert backend.calls[-1][1] == f"/api/library{path}"


@pytest.mark.asyncio
async def test_backend_tool_error_remains_visible_as_protocol_tool_failure(library):
    async with library() as (client, backend):
        backend.response = ToolError("Archived work not found (HTTP 404)")
        result = await client.call_tool("get_library_work", {"pixiv_id": 1, "kind": "novel"})
        assert result.is_error is True
        assert "HTTP 404" in " ".join(block.text for block in result.content if block.type == "text")
