import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

pytest.importorskip("mcp")
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.mcp.config import MCPSettings
from app.mcp.server import create_server


KEY = "mcp-protocol-test-" + "z" * 32
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_protocol_tools_resource_and_local_read(mode):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["X-API-Key"] == KEY
        return httpx.Response(200, json={"items": [{"pixiv_id": 5, "title": "本地作品"}],
                                       "pagination": {"page": 1, "page_size": 20}})

    server = create_server(MCPSettings(api_key=KEY, library_only=False), transport=httpx.MockTransport(handle))
    async with Client(server, mode=mode, read_timeout_seconds=10) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}
        assert {"archive_work", "get_library_work", "get_job", "read_novel_excerpt",
                "create_account_migration", "generate_ugoira_preview"} <= names
        assert len(names) == len(tools)
        assert all(tool.input_schema.get("type") == "object" for tool in tools)
        assert KEY not in str(tools)
        resources = (await client.list_resources()).resources
        assert any(str(resource.uri) == "pixiv://guide" for resource in resources)
        guide = await client.read_resource("pixiv://guide")
        assert "queued" in guide.contents[0].text
        result = await client.call_tool("search_library", {"kind": "novel", "q": "本地"})
        assert not result.is_error
        assert result.structured_content["items"][0]["title"] == "本地作品"
    assert len(seen) == 1
    assert seen[0].url.path == "/api/library/works"
    assert seen[0].url.params.get_list("type") == ["novel"]


async def test_read_only_discovery_hides_mutations_and_calls_are_blocked():
    calls = []
    server = create_server(MCPSettings(api_key=KEY, read_only=True, library_only=False), transport=httpx.MockTransport(
        lambda request: (calls.append(request), httpx.Response(200, json={}))[1]))
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert all(tool.annotations.read_only_hint for tool in tools)
        assert "archive_work" not in {tool.name for tool in tools}
        result = await client.call_tool("archive_work", {"kind": "novel", "pixiv_id": 5})
        assert result.is_error
    assert not calls


async def test_http_failure_is_protocol_tool_error_and_has_no_backend_secret():
    server = create_server(MCPSettings(api_key=KEY, library_only=False), transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="token=production-secret " + KEY)))
    async with Client(server) as client:
        result = await client.call_tool("get_library_stats")
        assert result.is_error
        assert "HTTP 401" in result.content[0].text
        assert "production-secret" not in str(result)
        assert KEY not in str(result)


async def test_tool_input_validation_prevents_backend_calls():
    def handle(request):
        pytest.fail("Invalid parameters reached backend")

    server = create_server(MCPSettings(api_key=KEY, library_only=False), transport=httpx.MockTransport(handle))
    async with Client(server) as client:
        for args in ({"pixiv_id": -1, "kind": "novel"}, {"pixiv_id": 1},
                     {"pixiv_id": 1, "kind": "../account"}):
            result = await client.call_tool("get_library_work", args)
            assert result.is_error


@pytest.fixture
def backend_http():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("X-API-Key")))
            payload = json.dumps({"works_total": 7, "works_by_type": {"novel": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


async def test_real_stdio_subprocess_and_http_backend_from_other_cwd(backend_http, tmp_path):
    """Exercise initialize/discover, JSON-RPC stdio, lifespan, and a real HTTP request."""
    url, seen = backend_http
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp", "--read-only"], cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
             "PIXIV_MCP_BACKEND_URL": url, "PIXIV_MCP_API_KEY": KEY},
    )
    async with Client(params, mode="legacy", read_timeout_seconds=15) as client:
        listed = await client.list_tools()
        assert "get_library_stats" in {tool.name for tool in listed.tools}
        assert all(tool.annotations.read_only_hint for tool in listed.tools)
        result = await client.call_tool("get_library_stats")
        assert not result.is_error
        assert result.structured_content["works_total"] == 7
    assert seen == [("GET", "/api/library/stats", KEY)]


def test_cli_reports_config_errors_on_stderr_without_secrets(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIXIV_MCP_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONUTF8="1", PIXIV_MCP_API_KEY=KEY,
               PIXIV_MCP_BACKEND_URL="http://user:do-not-leak@localhost")
    process = subprocess.run([sys.executable, "-m", "app.mcp"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert process.returncode == 2
    assert not process.stdout
    assert "backend_url" in process.stderr
    assert "do-not-leak" not in process.stderr and KEY not in process.stderr


def test_cli_help_and_version_need_no_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIXIV_MCP_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    for option, expected in (("--help", "stdio"), ("--version", "1.11.0")):
        process = subprocess.run([sys.executable, "-m", "app.mcp", option], cwd=tmp_path, env=env,
                                 capture_output=True, text=True, timeout=15)
        assert process.returncode == 0
        assert expected in process.stdout

