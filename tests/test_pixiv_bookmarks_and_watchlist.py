import httpx
import pytest
import requests

from app.clients.pixivpy_client import PixivPyClient
from app.clients.pixiv_web_client import PixivWebClient
from app.core.errors import PixivAuthError, PixivRemoteError, PixivRestrictedError, PixivUnavailableError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot


class Limiter:
    def __init__(self):
        self.waits = 0
        self.penalties = []
    async def wait(self):
        self.waits += 1
    async def penalize(self, retry_after=None):
        self.penalties.append(retry_after)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', [WorkType.ILLUST, WorkType.NOVEL])
async def test_bookmark_checks_only_detail_then_adds_private(monkeypatch, kind):
    client = PixivPyClient(rate_limiter=Limiter())
    calls = []
    async def call(method, *args, **kwargs):
        calls.append((method.__name__, args, kwargs))
        if len(calls) == 1:
            return {'id': 42, 'is_bookmarked': False}
        return {}
    monkeypatch.setattr(client, '_call', call)
    assert await client.ensure_bookmarked(42, kind) is True
    prefix = 'novel' if kind == WorkType.NOVEL else 'illust'
    assert [row[0] for row in calls] == [prefix + '_detail', prefix + '_bookmark_add']
    assert calls[1][2] == {'restrict': 'private'}


@pytest.mark.asyncio
async def test_existing_bookmark_snapshot_skips_read_and_write(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    async def forbidden(*args, **kwargs):
        pytest.fail('existing bookmark must not be read/written again')
    monkeypatch.setattr(client, '_call', forbidden)
    snapshot = RemoteSnapshot(pixiv_id=42, work_type=WorkType.NOVEL,
                              version_token='x', metadata={'is_bookmarked': True})
    assert await client.ensure_bookmarked(42, WorkType.NOVEL, snapshot=snapshot) is False


@pytest.mark.asyncio
async def test_missing_bookmark_state_never_writes(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    async def call(method, *args, **kwargs):
        assert method == client.api.novel_detail
        return {'id': 42}
    monkeypatch.setattr(client, '_call', call)
    with pytest.raises(PixivRemoteError, match='bookmark state'):
        await client.ensure_bookmarked(42, WorkType.NOVEL)


def test_novel_bookmark_uses_verified_app_endpoint(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client.api.set_auth('offline-test-access-token')
    sent = []
    def request(url, **kwargs):
        sent.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        return response
    monkeypatch.setattr(client.api.requests, 'post', request)
    client.api.novel_bookmark_add(42, restrict='private')
    assert len(sent) == 1
    assert sent[0][0] == 'https://app-api.pixiv.net/v2/novel/bookmark/add'
    assert sent[0][1]['data'] == {'novel_id': 42, 'restrict': 'private'}
    assert sent[0][1]['headers']['Authorization'] == 'Bearer offline-test-access-token'


@pytest.mark.asyncio
async def test_http_429_keeps_retry_after_and_shares_web_limiter(monkeypatch):
    limiter = Limiter()
    client = PixivPyClient(rate_limiter=limiter)
    client._next_auth_at = 10**12
    client.api.set_auth('offline-test-access-token')
    response = requests.Response()
    response.status_code = 429
    response.headers['Retry-After'] = '123'
    response._content = b'{}'
    monkeypatch.setattr(client.api.requests, 'get', lambda *a, **kw: response)
    with pytest.raises(PixivUnavailableError):
        await client._call(client.api.illust_detail, 42)
    assert limiter.penalties == ['123']
    assert client.web.rate_limiter is limiter


@pytest.mark.asyncio
async def test_network_url_id_cannot_be_mapped_to_deleted(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client._next_auth_at = 10**12
    def request():
        raise requests.ConnectionError('Failed URL /v1/illust/detail?illust_id=120404123')
    with pytest.raises(PixivUnavailableError):
        await client._call(request)


@pytest.mark.asyncio
async def test_structured_rate_limit_sets_cooldown_without_http_status():
    limiter = Limiter()
    client = PixivPyClient(rate_limiter=limiter)
    client._next_auth_at = 10**12
    with pytest.raises(PixivUnavailableError):
        await client._call(lambda: {'error': {'status_code': 429}})
    assert limiter.penalties == [None]


@pytest.mark.asyncio
async def test_auth_network_error_remains_retryable(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client.refresh_token = 'offline-test-refresh-token'
    def auth(**kwargs):
        raise requests.ConnectionError('temporary network failure')
    monkeypatch.setattr(client.api, 'auth', auth)
    with pytest.raises(PixivUnavailableError):
        await client._ensure_auth()


@pytest.mark.asyncio
async def test_web_429_applies_retry_after_without_anonymous_retry():
    limiter = Limiter()
    def handler(request):
        return httpx.Response(429, headers={'Retry-After': '180'})
    client = PixivWebClient('PHPSESSID=test', transport=httpx.MockTransport(handler), rate_limiter=limiter)
    with pytest.raises(PixivUnavailableError):
        await client.get_ugoira_metadata(42)
    assert limiter.waits == 1
    assert limiter.penalties == ['180']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 403])
async def test_app_http_error_with_structured_rate_limit_is_retryable(monkeypatch, status):
    limiter = Limiter()
    client = PixivPyClient(rate_limiter=limiter)
    client._next_auth_at = 10**12
    client.api.set_auth('offline-test-access-token')
    response = requests.Response()
    response.status_code = status
    response.headers['Retry-After'] = '90'
    response._content = b'{"error":{"reason":"rate_limit"}}'
    monkeypatch.setattr(client.api.requests, 'get', lambda *a, **kw: response)
    with pytest.raises(PixivUnavailableError, match='rate limit'):
        await client._call(client.api.illust_detail, 42)
    assert limiter.waits == 1
    assert limiter.penalties == ['90']


@pytest.mark.asyncio
async def test_ordinary_app_403_does_not_trigger_cooldown(monkeypatch):
    limiter = Limiter()
    client = PixivPyClient(rate_limiter=limiter)
    client._next_auth_at = 10**12
    client.api.set_auth('offline-test-access-token')
    response = requests.Response()
    response.status_code = 403
    response._content = b'{"error":{"message":"This work is private"}}'
    monkeypatch.setattr(client.api.requests, 'get', lambda *a, **kw: response)
    with pytest.raises(PixivRestrictedError):
        await client._call(client.api.illust_detail, 42)
    assert limiter.penalties == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status,payload', [
    (200, {'error': True, 'message': 'Rate Limit'}),
    (403, {'error': {'code': 'rate_limit'}}),
])
async def test_web_structured_rate_limit_cools_down_without_auth_misclassification(status, payload):
    limiter = Limiter()
    client = PixivWebClient('PHPSESSID=test', rate_limiter=limiter,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)))
    with pytest.raises(PixivUnavailableError, match='rate limit'):
        await client.get_followed_series_page('novel', 1)
    assert limiter.penalties == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['novel', 'manga'])
async def test_watchlist_reads_every_page_and_type(monkeypatch, kind):
    client = PixivPyClient(rate_limiter=Limiter())
    seen = []
    def handler(request):
        assert request.url.path == f'/ajax/watch_list/{kind}'
        assert request.url.params['new'] == '1'
        page = int(request.url.params['p'])
        seen.append(page)
        body = {'page': {'total': '2', 'maxPage': 2, 'watchedSeriesIds': [str(page)]}}
        batch = [{'id': str(page), 'title': f'series-{page}'}]
        if kind == 'novel':
            body['thumbnails'] = {'novelSeries': batch}
        else:
            body['illustSeries'] = batch
        return httpx.Response(200, json={'error': False, 'body': body})
    client.web = PixivWebClient('PHPSESSID=test', transport=httpx.MockTransport(handler), rate_limiter=Limiter())
    result = await client.discover_followed_series(kind)
    assert seen == [1, 2]
    assert [r['series_id'] for r in result] == [1, 2]
    assert all(r['series_type'] == kind for r in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'page': {'total': '2', 'maxPage': 1, 'watchedSeriesIds': ['1']}, 'thumbnails': {'novelSeries': [{'id': '1'}]}},
    {'page': {'total': '0', 'maxPage': 1, 'watchedSeriesIds': ['1']}},
    {'page': {'total': '1', 'maxPage': 1, 'watchedSeriesIds': ['1']}},
])
async def test_watchlist_rejects_partial_or_inconsistent_results(monkeypatch, body):
    client = PixivPyClient(rate_limiter=Limiter())
    async def page(*args):
        return body
    monkeypatch.setattr(client.web, 'get_followed_series_page', page)
    with pytest.raises(PixivRemoteError):
        await client.discover_followed_series()


@pytest.mark.asyncio
async def test_watchlist_requires_cookie_and_rejects_expired_cookie():
    with pytest.raises(PixivAuthError):
        await PixivWebClient('', rate_limiter=Limiter()).get_followed_series_page('novel', 1)
    client = PixivWebClient('PHPSESSID=expired', rate_limiter=Limiter(),
        transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    with pytest.raises(PixivAuthError):
        await client.get_followed_series_page('novel', 1)
