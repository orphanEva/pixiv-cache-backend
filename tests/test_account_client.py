import html
import json

import httpx
import pytest
import requests

from app.clients.pixiv_web_client import PixivWebClient, _web_identity_from_html
from app.clients.pixivpy_client import PixivPyClient
from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivUnavailableError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot


class Limiter:
    def __init__(self):
        self.penalties = []
        self.waits = 0
    async def wait(self):
        self.waits += 1
    async def penalize(self, retry_after=None):
        self.penalties.append(retry_after)


def identity_page(user_id='42', token='csrf-one', layout='next'):
    if layout == 'legacy':
        data = {'userData': {'id': user_id}, 'token': token}
        return f'<meta id="meta-global-data" content="{html.escape(json.dumps(data), quote=True)}">'
    if layout == 'vue':
        data = {'pixiv.user.id': user_id, 'pixiv.context.postKey': token}
        return f'<meta id="init-config" content="{html.escape(json.dumps(data), quote=True)}">'
    state = {'userData': {'self': {'id': user_id}}, 'api': {'token': token}}
    data = {'props': {'pageProps': {'serverSerializedPreloadedState': json.dumps(state)}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'


@pytest.mark.parametrize('layout', ['legacy', 'vue', 'next'])
def test_identity_and_csrf_from_supported_server_bootstraps(layout):
    assert _web_identity_from_html(identity_page(layout=layout)) == ('42', 'csrf-one')


def test_identity_parser_rejects_other_profile_ids_and_ambiguous_sessions():
    with pytest.raises(PixivAuthError):
        _web_identity_from_html('<a href="/users/42">someone</a>')
    with pytest.raises(PixivAuthError):
        _web_identity_from_html(identity_page('42') + identity_page('99', layout='legacy'))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['novel', 'manga'])
@pytest.mark.parametrize('watched', [False, True])
async def test_follow_uses_verified_account_csrf_and_skips_existing(kind, watched):
    posts = []
    def handler(request):
        if request.url.path == '/settings/account':
            return httpx.Response(200, text=identity_page())
        if request.method == 'GET':
            body = {'id': '17', 'isWatched': watched} if kind == 'novel' else {
                'page': {'seriesId': 17, 'isWatched': watched}}
            return httpx.Response(200, json={'error': False, 'body': body})
        posts.append(request)
        assert request.url.path == f"/ajax/{'novel' if kind == 'novel' else 'illust'}/series/17/watch"
        assert json.loads(request.content) == {}
        assert request.headers['x-csrf-token'] == 'csrf-one'
        assert request.headers['cookie'] == 'PHPSESSID=not-an-account-id'
        return httpx.Response(200, json={'error': False, 'body': []})
    client = PixivWebClient('PHPSESSID=not-an-account-id', rate_limiter=Limiter(),
                            transport=httpx.MockTransport(handler))
    assert await client.ensure_series_followed(kind, 17, expected_user_id=42) is (not watched)
    assert len(posts) == (0 if watched else 1)


@pytest.mark.asyncio
async def test_cookie_account_mismatch_never_posts_or_reads_series():
    requests_seen = []
    def handler(request):
        requests_seen.append(request.url.path)
        return httpx.Response(200, text=identity_page('99'))
    client = PixivWebClient('PHPSESSID=test', rate_limiter=Limiter(), transport=httpx.MockTransport(handler))
    with pytest.raises(PixivAuthError, match='mismatch'):
        await client.ensure_series_followed('novel', 17, expected_user_id='42')
    assert requests_seen == ['/settings/account']


@pytest.mark.asyncio
async def test_csrf_403_refresh_rechecks_identity_and_uses_new_token():
    tokens = []
    reads = 0
    def handler(request):
        nonlocal reads
        if request.url.path == '/settings/account':
            reads += 1
            return httpx.Response(200, text=identity_page(token=f'csrf-{reads}'))
        if request.method == 'GET':
            return httpx.Response(200, json={'error': False, 'body': {'id': '17', 'isWatched': False}})
        tokens.append(request.headers['x-csrf-token'])
        return httpx.Response(403) if len(tokens) == 1 else httpx.Response(200, json={'error': False, 'body': []})
    client = PixivWebClient('PHPSESSID=test', rate_limiter=Limiter(), transport=httpx.MockTransport(handler))
    assert await client.ensure_series_followed('novel', 17, expected_user_id=42)
    assert tokens == ['csrf-1', 'csrf-2']


@pytest.mark.asyncio
async def test_csrf_refresh_account_change_stops_second_write():
    identity_reads = 0
    posts = []
    def handler(request):
        nonlocal identity_reads
        if request.url.path == '/settings/account':
            identity_reads += 1
            return httpx.Response(200, text=identity_page('42' if identity_reads == 1 else '99'))
        if request.method == 'GET':
            return httpx.Response(200, json={'error': False, 'body': {'id': '17', 'isWatched': False}})
        posts.append(request)
        return httpx.Response(403)
    client = PixivWebClient('PHPSESSID=test', rate_limiter=Limiter(), transport=httpx.MockTransport(handler))
    with pytest.raises(PixivAuthError, match='changed'):
        await client.ensure_series_followed('novel', 17, expected_user_id=42)
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_series_write_rate_limit_does_not_refresh_csrf_or_retry():
    limiter = Limiter()
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == '/settings/account':
            return httpx.Response(200, text=identity_page())
        if request.method == 'GET':
            return httpx.Response(200, json={'error': False, 'body': {'id': '17', 'isWatched': False}})
        return httpx.Response(403, headers={'Retry-After': '600'}, json={'error': {'reason': 'rate_limit'}})
    client = PixivWebClient('PHPSESSID=test', rate_limiter=limiter, transport=httpx.MockTransport(handler))
    with pytest.raises(PixivUnavailableError):
        await client.ensure_series_followed('novel', 17, expected_user_id=42)
    assert len(paths) == 3
    assert limiter.penalties == ['600']


@pytest.mark.asyncio
async def test_identity_login_redirect_is_auth_failure_without_following_redirect():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={'Location': 'https://accounts.pixiv.net/login'})
    client = PixivWebClient('PHPSESSID=42_stale', rate_limiter=Limiter(), transport=httpx.MockTransport(handler))
    with pytest.raises(PixivAuthError):
        await client.get_session_identity()
    assert seen == ['/settings/account']


@pytest.mark.asyncio
async def test_cookie_change_invalidates_identity_and_csrf_cache():
    def handler(request):
        account = '42' if request.headers['Cookie'] == 'PHPSESSID=old' else '99'
        return httpx.Response(200, text=identity_page(account, token='token-' + account))
    client = PixivWebClient('PHPSESSID=old', rate_limiter=Limiter(), transport=httpx.MockTransport(handler))
    assert await client.get_session_identity() == {'user_id': '42'}
    client.set_cookie('PHPSESSID=new')
    assert await client.get_session_identity() == {'user_id': '99'}


@pytest.mark.asyncio
async def test_account_identity_force_does_not_force_oauth_refresh(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client.api.user_id = 42
    client._next_auth_at = 10**12
    def forbidden(**kwargs):
        pytest.fail('identity checks must not re-login every item')
    monkeypatch.setattr(client.api, 'auth', forbidden)
    client.web = PixivWebClient('PHPSESSID=test', rate_limiter=Limiter(),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=identity_page())))
    assert await client.get_account_identity(force=True) == {
        'user_id': '42', 'app_user_id': '42', 'web_user_id': '42', 'same_account': True}


@pytest.mark.asyncio
async def test_bookmark_target_guard_runs_again_after_detail(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client.api.user_id = 42
    client._next_auth_at = 10**12
    client.api.set_auth('offline-test-token')
    posts = []
    def read(*args, **kwargs):
        client.api.user_id = 99
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"illust":{"id":17,"is_bookmarked":false}}'
        return response
    monkeypatch.setattr(client.api.requests, 'get', read)
    monkeypatch.setattr(client.api.requests, 'post', lambda *a, **kw: posts.append(kw))
    with pytest.raises(PixivAuthError, match='changed'):
        await client.ensure_bookmarked(17, WorkType.ILLUST, expected_user_id=42)
    assert posts == []


@pytest.mark.asyncio
async def test_migration_ignores_old_account_snapshot_bookmark_state(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    client.api.user_id = 42
    client._next_auth_at = 10**12
    calls = []
    async def call(method, *args, **kwargs):
        calls.append(method.__name__)
        assert kwargs['_expected_user_id'] == '42'
        return {'id': 17, 'is_bookmarked': False} if len(calls) == 1 else {}
    monkeypatch.setattr(client, '_call', call)
    snapshot = RemoteSnapshot(pixiv_id=17, work_type=WorkType.NOVEL,
                               version_token='old', metadata={'is_bookmarked': True})
    assert await client.ensure_bookmarked(17, WorkType.NOVEL, snapshot=snapshot, expected_user_id='42')
    assert calls == ['novel_detail', 'novel_bookmark_add']


@pytest.mark.asyncio
async def test_explicit_reload_is_blocked_while_write_operation_is_active():
    client = PixivPyClient(rate_limiter=Limiter())
    async with client._credentials_lock:
        with pytest.raises(PixivUnavailableError, match='active'):
            await client.reload_credentials()


@pytest.mark.asyncio
async def test_explicit_reload_resets_sdk_identity_and_web_csrf(monkeypatch):
    client = PixivPyClient(rate_limiter=Limiter())
    old_api = client.api
    client.web._session_user_id, client.web._csrf_token = '42', 'old-csrf'
    monkeypatch.setattr(get_settings(), 'pixiv_refresh_token', 'new-test-refresh-token')
    monkeypatch.setattr(get_settings(), 'pixiv_cookie', 'PHPSESSID=new-test-cookie')
    await client.reload_credentials()
    assert client.api is not old_api
    assert client.refresh_token == 'new-test-refresh-token'
    assert client.web.cookie == 'PHPSESSID=new-test-cookie'
    assert client.web._session_user_id is None and client.web._csrf_token is None
    assert client._next_auth_at == 0
