from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api.account_migrations import get_migration_service
from app.core.errors import PixivAuthError, PixivUnavailableError


def test_account_migration_api_auth_target_and_safe_errors(monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "S" * 32)
    service = SimpleNamespace(
        create_run=AsyncMock(return_value={"run_id": 7, "target_user_id": "123"}),
        get_run=AsyncMock(side_effect=LookupError()),
        retry_failed=AsyncMock(side_effect=PixivAuthError("token=secret-must-not-leak")),
    )
    main.app.dependency_overrides[get_migration_service] = lambda: service
    try:
        client = TestClient(main.app)
        assert client.post("/api/account/migrations").status_code == 401
        service.create_run.assert_not_awaited()
        response = client.post("/api/account/migrations", headers={"X-API-Key": "S" * 32},
                               json={"target_user_id": "999"})
        assert response.status_code == 202
        assert response.json()["target_user_id"] == "123"
        service.create_run.assert_awaited_once_with()
        assert client.get("/api/account/migrations/7", headers={"X-API-Key": "S" * 32}).status_code == 404
        response = client.post("/api/account/migrations/7/retry", headers={"X-API-Key": "S" * 32})
        assert response.status_code == 401
        assert "secret" not in response.text
        service.create_run.side_effect = PixivUnavailableError("busy")
        assert client.post("/api/account/migrations", headers={"X-API-Key": "S" * 32}).status_code == 503
    finally:
        main.app.dependency_overrides.pop(get_migration_service, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    {"status": "account_changed", "target_user_id": "123", "activation_succeeded": False},
    {"status": "idle", "target_user_id": "456", "activation_succeeded": True},
])
async def test_worker_activates_once_then_stops_when_account_superseded(monkeypatch, replacement, maintenance_redis):
    from app.workers import sync_worker as module

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    service = SimpleNamespace(tick=AsyncMock(side_effect=[
        {"status": "running", "target_user_id": "123", "activation_succeeded": True}, replacement,
    ]))
    monkeypatch.setattr(module, "SessionLocal", SessionContext)
    monkeypatch.setattr(module, "AccountMigrationService", lambda *args: service)
    worker = module.SyncWorker.__new__(module.SyncWorker)
    worker.settings = SimpleNamespace(account_migration_enabled=True, account_migration_tick_seconds=1)
    worker.redis, worker.client = maintenance_redis, object()
    worker._last_account_migration = 0
    worker._account_activation_done = False
    worker._activated_account_id = None
    worker._account_superseded = False
    await worker._migrate_account_if_needed()
    assert worker._account_activation_done
    assert worker._activated_account_id == "123"
    worker._last_account_migration = 0
    await worker._migrate_account_if_needed()
    assert worker._account_superseded
    worker._last_account_migration = 0
    await worker._migrate_account_if_needed()
    assert [call.kwargs for call in service.tick.await_args_list] == [
        {"allow_account_switch": True}, {"allow_account_switch": False},
    ]


@pytest.mark.asyncio
async def test_worker_does_not_reactivate_after_batch_failure(monkeypatch, maintenance_redis):
    from app.workers import sync_worker as module

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    calls = []

    class FailingBatch:
        activation_attempted = False
        activation_target = None

        async def tick(self, *, allow_account_switch):
            calls.append(allow_account_switch)
            if allow_account_switch:
                self.activation_attempted = True
                self.activation_target = "123"
                raise ConnectionError("COMMIT response lost or later batch failed")
            return {"status": "account_changed", "target_user_id": "123", "activation_succeeded": False}

    monkeypatch.setattr(module, "SessionLocal", SessionContext)
    monkeypatch.setattr(module, "AccountMigrationService", lambda *args: FailingBatch())
    worker = module.SyncWorker.__new__(module.SyncWorker)
    worker.settings = SimpleNamespace(account_migration_enabled=True, account_migration_tick_seconds=1)
    worker.redis, worker.client = maintenance_redis, object()
    worker._last_account_migration = 0
    worker._account_activation_done = False
    worker._activated_account_id = None
    worker._account_superseded = False
    await worker._migrate_account_if_needed()
    assert worker._account_activation_done
    assert worker._activated_account_id == "123"
    worker._last_account_migration = 0
    await worker._migrate_account_if_needed()
    assert worker._account_superseded
    assert calls == [True, False]
