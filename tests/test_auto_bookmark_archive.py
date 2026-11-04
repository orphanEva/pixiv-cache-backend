from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.errors import PixivRestrictedError, PixivUnavailableError
from app.models.work import WorkType, WorkStatus
from sqlalchemy.exc import OperationalError
from test_cache_service import FakePixiv, FakeRedis, FakeSession, MemoryCacheService, snapshot


class BookmarkClient(FakePixiv):
    def __init__(self, snapshots, events, error=None):
        super().__init__(snapshots)
        self.events = events
        self.error = error

    async def ensure_bookmarked(self, pixiv_id, work_type, *, restrict="private", snapshot=None):
        self.events.append(("bookmark", pixiv_id, work_type, restrict))
        if self.error:
            raise self.error
        return True

    async def _next(self):
        self.events.append(("fetch_content",))
        return await super()._next()


@pytest.fixture
def cache_factory(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "storage_min_free_bytes", 0)
    monkeypatch.setattr(settings, "remote_check_ttl_seconds", 0)
    monkeypatch.setattr(settings, "pixiv_auto_bookmark_restrict", "private")

    def build(values, error=None):
        events = []
        client = BookmarkClient(values, events, error)
        service = MemoryCacheService(FakeSession(), client, redis_client=FakeRedis())
        original = service.storage.materialize

        async def materialize(value, version):
            events.append(("write_files",))
            return await original(value, version)

        monkeypatch.setattr(service.storage, "materialize", materialize)
        return service, events

    return build


@pytest.mark.asyncio
async def test_uncached_novel_bookmarks_privately_before_fetching_text(cache_factory):
    service, events = cache_factory([snapshot("a" * 64, "body")])
    result = await service.get_novel(123, bookmark_if_missing=True)
    assert result.version == 1
    assert events == [("bookmark", 123, WorkType.NOVEL, "private"), ("fetch_content",), ("write_files",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [WorkType.ILLUST, WorkType.MANGA])
async def test_visual_work_bookmarked_before_media_download(cache_factory, monkeypatch, kind):
    import httpx
    from app.schemas.pixiv import RemoteSnapshot, RemoteAsset
    value = RemoteSnapshot(pixiv_id=123, work_type=kind, title="wallpaper or manga",
        version_token="a" * 64,
        assets=[RemoteAsset(page_index=0, url="https://i.pximg.net/123.jpg", filename="0.jpg")])
    service, events = cache_factory([value])
    class Result:
        def scalar_one_or_none(self): return None
    previous_execute = service.session.execute
    async def execute(statement):
        if str(statement).startswith("SELECT"): return Result()
        return await previous_execute(statement)
    monkeypatch.setattr(service.session, "execute", execute)
    async def download(request):
        events.append(("media_request",))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"image bytes")
    monkeypatch.setattr(service.storage, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(download)))
    await service._get(123, kind, refresh=True, bookmark_if_missing=True)
    assert events[0] == ("bookmark", 123, kind, "private")
    assert [event[0] for event in events] == ["bookmark", "fetch_content", "write_files", "media_request"]


@pytest.mark.asyncio
async def test_existing_archive_does_not_bookmark_again(cache_factory):
    service, events = cache_factory([snapshot("a" * 64, "body"), snapshot("a" * 64, "body")])
    await service.get_novel(123)
    events.clear()
    await service.get_novel(123, refresh=False, bookmark_if_missing=True)
    assert events == []
    await service.get_novel(123, refresh=True, bookmark_if_missing=True)
    assert events == [("fetch_content",)]


@pytest.mark.asyncio
async def test_automatic_source_archive_does_not_bookmark(cache_factory):
    service, events = cache_factory([snapshot("a" * 64, "body")])
    await service._get(123, WorkType.NOVEL, refresh=True)
    assert [event[0] for event in events] == ["fetch_content", "write_files"]


@pytest.mark.asyncio
async def test_bookmark_failure_prevents_content_download(cache_factory, tmp_path):
    service, events = cache_factory([snapshot("a" * 64, "body")], PixivUnavailableError("limited"))
    with pytest.raises(PixivUnavailableError):
        await service.get_novel(123, bookmark_if_missing=True)
    assert [event[0] for event in events] == ["bookmark"]
    assert service.session.work is None
    assert not list(tmp_path.rglob("novel.txt"))


@pytest.mark.asyncio
async def test_timeout_does_not_reopen_revoked_cache(cache_factory):
    service, _ = cache_factory([snapshot("a" * 64, "body"), PixivRestrictedError("private"), PixivUnavailableError("timeout")])
    await service.get_novel(123)
    with pytest.raises(PixivRestrictedError):
        await service.get_novel(123)
    with pytest.raises(PixivUnavailableError):
        await service.get_novel(123)
    assert service.session.work.status == WorkStatus.RESTRICTED
    with pytest.raises(PixivRestrictedError):
        await service.get_novel(123, refresh=False)


@pytest.mark.asyncio
async def test_unchanged_content_updates_current_metadata(cache_factory):
    first = snapshot("a" * 64, "body")
    second = snapshot("a" * 64, "body")
    second.x_restrict = 1
    second.is_ai = True
    second.author_name = "updated author"
    service, _ = cache_factory([first, second])
    await service.get_novel(123)
    result = await service.get_novel(123)
    assert result.version == 1
    assert service.session.work.x_restrict == 1
    assert service.session.work.is_ai is True
    assert service.session.work.author_name == "updated author"


@pytest.mark.asyncio
async def test_uncertain_commit_preserves_materialized_files(cache_factory, tmp_path, monkeypatch):
    service, _ = cache_factory([snapshot("a" * 64, "body")])
    async def uncertain_commit():
        raise OperationalError("COMMIT", {}, ConnectionError("response lost"))
    monkeypatch.setattr(service.session, "commit", uncertain_commit)
    with pytest.raises(OperationalError):
        await service.get_novel(123)
    assert (tmp_path / "novel/123/versions/1/novel.txt").read_text(encoding="utf-8") == "body"


@pytest.mark.asyncio
async def test_registered_version_is_never_quarantined(cache_factory, tmp_path, monkeypatch):
    service, events = cache_factory([snapshot("a" * 64, "body")])
    target = tmp_path / "novel/123/versions/1"
    target.mkdir(parents=True)
    (target / "novel.txt").write_text("already committed", encoding="utf-8")
    class Result:
        def scalar_one_or_none(self): return 123
    async def execute(statement): return Result()
    monkeypatch.setattr(service.session, "execute", execute)
    with pytest.raises(PixivUnavailableError, match="already committed"):
        await service.get_novel(123)
    assert (target / "novel.txt").read_text(encoding="utf-8") == "already committed"
    assert ("write_files",) not in events
    assert not (tmp_path / ".quarantine").exists()


@pytest.mark.asyncio
async def test_download_hooks_share_pacing_and_report_429():
    import httpx
    from app.storage.local_storage import LocalStorage
    events = []
    class Limiter:
        async def wait(self): events.append("wait")
        async def penalize(self, value): events.append(("cooldown", value))
    storage = LocalStorage()
    storage.rate_limiter = Limiter()
    async with storage._http_client() as client:
        request_hook = client.event_hooks["request"][0]
        response_hook = client.event_hooks["response"][0]
        await request_hook(httpx.Request("GET", "https://i.pximg.net/example.jpg"))
        await response_hook(httpx.Response(429, headers={"Retry-After": "600"}))
    assert events == ["wait", ("cooldown", "600")]
