"""API-key protected migration controls; credentials are configured server-side."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import get_client
from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import PixivRemoteError
from app.core.redis_client import get_redis
from app.db.session import get_session
from app.models.work import AccountMigrationItem, AccountMigrationRun
from app.services.account_migration import AccountMigrationService

router = APIRouter(prefix="/api/account", tags=["account-migration"])


def get_migration_service(
    session: AsyncSession = Depends(get_session),
    client: PixivPyClient = Depends(get_client),
) -> AccountMigrationService:
    return AccountMigrationService(session, client, get_redis())


def _remote_error(exc: PixivRemoteError) -> None:
    # Do not expose upstream payloads or credentials through an admin endpoint.
    raise HTTPException(exc.status_code, {
        "error_type": type(exc).__name__,
        "message": "Account operation could not complete; check credentials or retry later",
    }) from exc


@router.get("/identity")
async def account_identity(client: PixivPyClient = Depends(get_client)):
    try:
        return await client.get_account_identity(force=True)
    except PixivRemoteError as exc:
        _remote_error(exc)


@router.post("/migrations", status_code=202)
async def create_migration(service: AccountMigrationService = Depends(get_migration_service)):
    """Rescan local works/series for the current verified account only."""
    try:
        return await service.create_run()
    except PixivRemoteError as exc:
        _remote_error(exc)


@router.get("/migrations")
async def list_migrations(
    limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_session),
    service: AccountMigrationService = Depends(get_migration_service),
):
    stmt = select(AccountMigrationRun.id).order_by(AccountMigrationRun.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AccountMigrationRun.id < before_id)
    ids = (await session.execute(stmt)).scalars().all()
    return {"items": [await service.get_run(run_id) for run_id in ids],
            "next_before_id": ids[-1] if len(ids) == limit else None}


@router.get("/migrations/{run_id}")
async def get_migration(run_id: int, service: AccountMigrationService = Depends(get_migration_service)):
    try:
        return await service.get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, "Account migration not found") from exc


@router.get("/migrations/{run_id}/items")
async def list_migration_items(
    run_id: int,
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    if await session.get(AccountMigrationRun, run_id) is None:
        raise HTTPException(404, "Account migration not found")
    stmt = select(AccountMigrationItem).where(AccountMigrationItem.run_id == run_id)
    if status is not None:
        stmt = stmt.where(AccountMigrationItem.status == status)
    rows = (await session.execute(stmt.order_by(
        AccountMigrationItem.kind, AccountMigrationItem.target_id,
    ).offset(offset).limit(limit))).scalars().all()
    return {"items": [{name: getattr(row, name) for name in (
        "kind", "target_id", "status", "attempts", "next_attempt_at",
        "last_error_type", "last_error", "updated_at", "finished_at",
    )} for row in rows], "next_offset": offset + limit if len(rows) == limit else None}


@router.post("/migrations/{run_id}/retry", status_code=202)
async def retry_migration(run_id: int, service: AccountMigrationService = Depends(get_migration_service)):
    try:
        return await service.retry_failed(run_id)
    except LookupError as exc:
        raise HTTPException(404, "Account migration not found") from exc
    except PixivRemoteError as exc:
        _remote_error(exc)
