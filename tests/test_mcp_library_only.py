"""Offline regression tests for the default no-upstream MCP boundary."""
import httpx
import json
from pathlib import Path
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.backend import BackendClient
from app.mcp.config import MCPSettings
from app.mcp.server import create_server

KEY = "offline-library-test-" + "z" * 32


async def test_default_discovery_excludes_all_action_tools_and_blocks_calls():
    seen = []
    settings = MCPSettings(api_key=KEY)
    assert settings.library_only is True
    server = create_server(settings, transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={}))[1]))
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "search_library" in names
        assert "get_library_stats" in names
        for name, args in [
            ("account_identity", {}), ("list_jobs", {}), ("run_sync_source", {"source_id": 1}),
            ("archive_work", {"kind": "novel", "pixiv_id": 1}),
        ]:
            assert name not in names
            assert (await client.call_tool(name, args)).is_error
        guide = await client.read_resource("pixiv://guide")
        assert "不启动或恢复" in guide.contents[0].text
    assert seen == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/account/identity"), ("GET", "/api/jobs"),
    ("GET", "/api/sync/sources"), ("POST", "/api/library/works/1/refresh"),
    ("GET", "/api/library/works/1/refresh"), ("POST", "/api/library/stats"),
    ("GET", "/api/library/future-remote-endpoint"),
])
async def test_library_boundary_rejects_non_allowlisted_paths_before_io(method, path):
    def reject(request):
        pytest.fail("Forbidden request reached transport")
    async with BackendClient(MCPSettings(api_key=KEY), transport=httpx.MockTransport(reject)) as backend:
        with pytest.raises(ToolError):
            await backend.request(method, path)


@pytest.mark.parametrize("path", [
    "/api/library/works", "/api/library/works/1", "/api/library/works/1/history",
    "/api/library/works/1/versions/2", "/api/library/works/1/integrity",
    "/api/library/authors", "/api/library/authors/1", "/api/library/tags",
    "/api/library/series", "/api/library/series/novel/1", "/api/library/stats",
])
async def test_allowlisted_local_routes(path):
    seen = []
    async with BackendClient(MCPSettings(api_key=KEY), transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={}))[1])) as backend:
        assert await backend.request("GET", path) == {}
    assert len(seen) == 1
    assert seen[0].url.path == path


def test_local_mode_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("PIXIV_MCP_LIBRARY_ONLY", "false")
    assert MCPSettings(api_key=KEY).library_only is False
    assert MCPSettings(api_key=KEY, library_only=True).library_only is True


async def test_documented_full_client_exposes_capabilities_without_backend_io(monkeypatch):
    example = Path(__file__).resolve().parents[1] / "examples" / "mcp-client.json"
    config = json.loads(example.read_text())["mcpServers"]["pixiv-cache"]
    for key, value in config["env"].items():
        monkeypatch.setenv(key, value)
    settings = MCPSettings()
    assert settings.library_only is False
    assert settings.read_only is False
    def reject(request):
        pytest.fail("Discovery must not contact the backend or upstream")
    server = create_server(settings, transport=httpx.MockTransport(reject))
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"search_library", "archive_work", "archive_series", "account_identity",
                "get_job", "run_sync_source", "patch_sync_source"} <= names
        await client.read_resource("pixiv://guide")
