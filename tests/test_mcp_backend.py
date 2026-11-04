import asyncio

import httpx
import pytest
from pydantic import ValidationError

pytest.importorskip("mcp")
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.backend import BackendClient
from app.mcp.config import MCPSettings


KEY = "test-only-mcp-key-" + "x" * 32


def settings(**overrides):
    return MCPSettings(api_key=KEY, **overrides)


@pytest.mark.parametrize("url", [
    "file:///tmp/a", "http://user:password@localhost", "http://localhost?key=x",
    "http://localhost#secret", "http://localhost:abc", "http://localhost:0",
    "http://localhost/../api", "http://localhost/%2e%2e", "http://local\\host", " http://localhost",
])
def test_reject_ambiguous_or_credential_urls(url):
    with pytest.raises(ValidationError):
        settings(backend_url=url)


@pytest.mark.parametrize("key", ["", "short", "x" * 32 + "\nsecret", "密" * 32])
def test_invalid_api_key_is_not_in_error_or_repr(key):
    with pytest.raises(ValidationError) as exc:
        MCPSettings(api_key=key)
    assert "input_value" not in str(exc.value)
    assert KEY not in repr(settings())


def test_config_does_not_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXIV_MCP_API_KEY", raising=False)
    (tmp_path / ".env").write_text(f"PIXIV_MCP_API_KEY={KEY}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        MCPSettings()


async def test_backend_prefix_query_auth_and_redaction():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [{"title": "日本語", "api_key": "hidden",
            "note": f"key={KEY}", "access_token": "hidden", "count": 1}]})

    async with BackendClient(settings(backend_url="http://localhost/proxy"),
                             transport=httpx.MockTransport(handle)) as client:
        result = await client.request("GET", "/api/library/works", params={
            "q": "日本語", "type": ["illust", "manga"], "none": None, "is_ai": False})
    assert seen[0].url.path == "/proxy/api/library/works"
    assert seen[0].headers["X-API-Key"] == KEY
    assert seen[0].url.params.get_list("type") == ["illust", "manga"]
    assert seen[0].url.params["is_ai"] == "false"
    assert "none" not in seen[0].url.params
    assert result["items"][0]["title"] == "日本語"
    assert KEY not in str(result) and "hidden" not in str(result)
    assert client._client is None


@pytest.mark.parametrize("path", ["https://evil.test/api/test", "//evil.test/api/test", "/api/../x",
    "/api/test?api_key=secret", "/api/test#x", "/api/%2e%2e/x", "/api/foo//bar", "/etc/passwd"])
async def test_internal_route_validation_never_calls_transport(path):
    def handle(request):
        pytest.fail("Invalid path reached network")

    async with BackendClient(settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError):
            await client.request("GET", path)


async def test_read_only_client_refuses_changes():
    def handle(request):
        pytest.fail("Read-only mode submitted a change")

    async with BackendClient(settings(read_only=True), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError, match="只读"):
            await client.request("POST", "/api/account/migrations")


@pytest.mark.parametrize("status", [301, 401, 403, 404, 409, 422, 429, 500, 503])
async def test_errors_hide_backend_bodies_and_never_retry(status):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "http://evil.test", "Retry-After": "45"},
                              text=f"password=backend-secret {KEY}")

    async with BackendClient(settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError) as exc:
            await client.request("POST", "/api/account/migrations")
    assert f"HTTP {status}" in str(exc.value)
    assert "45" in str(exc.value)
    assert KEY not in str(exc.value) and "backend-secret" not in str(exc.value)
    assert "evil.test" not in str(exc.value)
    assert len(seen) == 1


async def test_write_timeout_reports_unknown_outcome_without_retry():
    seen = []

    def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout(f"secret {KEY}", request=request)

    async with BackendClient(settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError, match="可能已被接受") as exc:
            await client.request("POST", "/api/account/migrations")
    assert KEY not in str(exc.value) and len(seen) == 1


async def test_lost_write_connection_reports_unknown_outcome():
    def handle(request):
        raise httpx.RemoteProtocolError("connection dropped after request was sent")

    async with BackendClient(settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError, match="操作结果尚未确认"):
            await client.request("POST", "/api/account/migrations")


@pytest.mark.parametrize("body", [b"<html>secret backend error</html>", b"[]", b"null"])
async def test_malformed_backend_response(body):
    async with BackendClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body))) as client:
        with pytest.raises(ToolError) as exc:
            await client.request("GET", "/api/library/stats")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"x" * 2000])
async def test_unreadable_successful_write_response_requires_status_check(body):
    async with BackendClient(settings(max_response_bytes=1024), transport=httpx.MockTransport(
        lambda request: httpx.Response(202, content=body))) as client:
        with pytest.raises(ToolError, match="后端可能已经接受操作"):
            await client.request("POST", "/api/account/migrations")


async def test_stream_response_limit_closes_response():
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 800
            yield b"x" * 800
            pytest.fail("Read beyond size limit")

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with BackendClient(settings(max_response_bytes=1024), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(ToolError, match="大小上限"):
            await client.request("GET", "/api/library/stats")
    assert stream.closed


async def test_cancellation_propagates_and_closes_client():
    async def handle(request):
        raise asyncio.CancelledError()

    client = BackendClient(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(asyncio.CancelledError):
        async with client:
            await client.request("POST", "/api/account/migrations")
    assert client._client is None
