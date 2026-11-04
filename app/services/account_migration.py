"""Resume account migration from MySQL without loading the entire local archive."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivNotFoundError, PixivRestrictedError, PixivUnavailableError
from app.core.maintenance import maintenance_active
from app.core.redis_lock import RenewingRedisLock
from app.models.work import (
    AccountMigrationItem, AccountMigrationRun, AccountMigrationState,
    PixivWork, Series, SyncSource, SyncSourceType, WorkType,
)
from app.services.cache_service import now_utc


OPEN_RUN_STATUSES = ("pending", "running", "paused")
ITEM_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")


class AccountMigrationService:
    """Bookmark works privately and follow series for a verified target account.

    The global coordinator serializes activation with short processing batches;
    the renewable per-account lock additionally identifies the current writer.
    Persisting an item as running before its remote call makes process death or
    an uncertain commit safe to replay through the client's ensure operations.
    """

    def __init__(self, session, client, redis_client):
        self.session = session
        self.client = client
        self.redis = redis_client
        self.settings = get_settings()
        # The worker must consume its startup-only switch permission even if
        # activation commits and a later stage of this tick fails to return.
        self.activation_attempted = False
        self.activation_target: str | None = None

    @asynccontextmanager
    async def _guard(self, suffix: str):
        lock = RenewingRedisLock(
            self.redis, f"pixiv:account-migration:{suffix}",
            ttl_seconds=self.settings.account_migration_lock_ttl_seconds,
            wait_seconds=0,
        )
        acquired = await lock.acquire()
        try:
            yield lock if acquired else None
        finally:
            if acquired:
                await lock.release()

    @staticmethod
    def _account_id(identity: dict) -> str:
        value = str(identity.get("app_user_id") or identity.get("user_id") or "")
        if not value.isdigit() or int(value) <= 0:
            raise PixivAuthError("Pixiv did not return a verified account ID")
        user_id = identity.get("user_id")
        web_id = identity.get("web_user_id")
        if (user_id is not None and str(user_id) != value) or (
            web_id is not None and str(web_id) != value
        ) or identity.get("same_account") is False:
            raise PixivAuthError("Pixiv App and Web credentials belong to different accounts")
        return value

    async def _identity(self, *, force: bool, expected: str | None = None) -> str:
        identity = await self.client.get_account_identity(force=force)
        account_id = self._account_id(identity)
        if expected is not None and account_id != expected:
            raise PixivAuthError("Pixiv account changed; migration was paused")
        return account_id

    async def _pause_runs(self, *, target_user_id: str | None, reason: str) -> None:
        statement = update(AccountMigrationRun).where(
            AccountMigrationRun.status.in_(OPEN_RUN_STATUSES),
        )
        if target_user_id is not None:
            statement = statement.where(AccountMigrationRun.target_user_id == target_user_id)
        await self.session.execute(statement.values(status="paused", last_error=reason, updated_at=now_utc()))
        await self.session.commit()

    async def _activate(self, target: str, *, allow_account_switch: bool) -> bool:
        state = await self.session.get(AccountMigrationState, 1)
        if state is None:
            self.session.add(AccountMigrationState(id=1, active_user_id=target, activated_at=now_utc()))
        elif state.active_user_id != target:
            if not allow_account_switch:
                await self._pause_runs(target_user_id=target, reason="Another account is active")
                return False
            state.active_user_id = target
            state.activated_at = now_utc()
        await self.session.execute(update(AccountMigrationRun).where(
            AccountMigrationRun.target_user_id != target,
            AccountMigrationRun.status.in_(OPEN_RUN_STATUSES),
        ).values(status="paused", last_error="Another account is active", updated_at=now_utc()))
        await self.session.commit()
        return True

    async def _new_run(self, target: str, *, automatic: bool) -> AccountMigrationRun:
        now = now_utc()
        run = AccountMigrationRun(
            target_user_id=target, automatic_key=target if automatic else None,
            status="pending", scan_phase="works", scan_cursor={},
            created_at=now, updated_at=now, finished_at=None, last_error=None,
        )
        self.session.add(run)
        await self.session.commit()
        return run

    async def _ensure_automatic_run(self, target: str, *, rescan_completed: bool) -> AccountMigrationRun:
        run = (await self.session.execute(select(AccountMigrationRun).where(
            AccountMigrationRun.automatic_key == target,
        ))).scalar_one_or_none()
        if run is None:
            return await self._new_run(target, automatic=True)
        if rescan_completed and run.status == "completed":
            # Existing item keys/successes survive, while newly archived works
            # and newly configured series sources are discovered in pages.
            run.status = "pending"
            run.scan_phase = "works"
            run.scan_cursor = {}
            run.finished_at = None
            run.last_error = None
            run.updated_at = now_utc()
            await self.session.commit()
        return run

    async def create_run(self, target_user_id: str | None = None, *, automatic: bool = False) -> dict:
        """A manual rescan can target only the currently authenticated account."""
        async with self._guard("coordinator") as guard:
            if guard is None:
                raise PixivUnavailableError("An account migration batch is already running")
            await self.session.rollback()
            target = await self._identity(force=True)
            if target_user_id is not None and str(target_user_id) != target:
                raise PixivAuthError("Migration target must match the current Pixiv account")
            guard.ensure_alive()
            await self._activate(target, allow_account_switch=True)
            run = (await self._ensure_automatic_run(target, rescan_completed=True)
                   if automatic else await self._new_run(target, automatic=False))
            return await self.get_run(run.id)

    async def tick(self, *, allow_account_switch: bool = False) -> dict[str, Any]:
        """Discover one bounded page and process at most one bounded item batch."""
        if not self.settings.account_migration_enabled:
            return {"status": "disabled", "activation_succeeded": False}
        if await maintenance_active(self.redis):
            return {"status": "maintenance", "activation_succeeded": False}
        async with self._guard("coordinator") as coordinator:
            if coordinator is None:
                return {"status": "busy", "activation_succeeded": False}
            # A service/session can be reused between ticks. End any previous
            # read snapshot before observing the account activated by a worker.
            await self.session.rollback()
            if not allow_account_switch:
                state = await self.session.get(AccountMigrationState, 1)
                if state is not None:
                    open_run = (await self.session.execute(select(AccountMigrationRun.id).where(
                        AccountMigrationRun.target_user_id == state.active_user_id,
                        AccountMigrationRun.status.in_(OPEN_RUN_STATUSES),
                    ).limit(1))).scalar_one_or_none()
                    if open_run is None:
                        # Credential switches are activated at process startup
                        # or by an explicit manual run. An idle worker need not
                        # reread the remote account settings every tick.
                        return {"status": "idle", "target_user_id": state.active_user_id,
                                "activation_succeeded": True}
            try:
                target = await self._identity(force=True)
            except PixivAuthError:
                # We do not know which account these failing credentials belong
                # to. In particular, an expired old worker must not pause the
                # new account's run merely because it can read the active row.
                return {"status": "auth_required", "activation_succeeded": False}
            async with self._guard(f"account:{target}") as account_guard:
                if account_guard is None:
                    return {"status": "busy", "activation_succeeded": False}
                coordinator.ensure_alive()
                account_guard.ensure_alive()
                if allow_account_switch:
                    # Set BEFORE the commit attempt: a lost DB response also
                    # consumes permission rather than allowing a stale worker
                    # to reactivate its old account after another worker wins.
                    self.activation_attempted = True
                    self.activation_target = target
                if not await self._activate(target, allow_account_switch=allow_account_switch):
                    return {"status": "account_changed", "target_user_id": target, "activation_succeeded": False}
                await self._ensure_automatic_run(target, rescan_completed=allow_account_switch)
                run = (await self.session.execute(select(AccountMigrationRun).where(
                    AccountMigrationRun.target_user_id == target,
                    AccountMigrationRun.status.in_(OPEN_RUN_STATUSES),
                ).order_by(AccountMigrationRun.id).limit(1))).scalar_one_or_none()
                if run is None:
                    return {"status": "idle", "target_user_id": target, "activation_succeeded": True}
                run_id = run.id
                run.status = "running"
                run.last_error = None
                run.updated_at = now_utc()
                await self.session.commit()
                discovered = await self._discover_page(run)
                items = (await self.session.execute(select(AccountMigrationItem).where(
                    AccountMigrationItem.run_id == run_id, self._ready_predicate(),
                ).order_by(AccountMigrationItem.kind, AccountMigrationItem.target_id)
                 .limit(self.settings.account_migration_batch_size))).scalars().all()
                # End the read transaction before network calls/credential guards.
                await self.session.commit()
                processed = 0
                for item in items:
                    coordinator.ensure_alive()
                    account_guard.ensure_alive()
                    if not await self._process_item(run, item, coordinator, account_guard):
                        break
                    processed += 1
                if run.status != "paused":
                    await self._finish_if_done(run)
                result = await self.get_run(run_id)
                result.update(activation_succeeded=True, discovered_this_tick=discovered, processed_this_tick=processed)
                return result

    def _ready_predicate(self):
        return or_(
            AccountMigrationItem.status.in_(("pending", "running")),
            and_(AccountMigrationItem.status == "failed",
                 AccountMigrationItem.attempts < self.settings.account_migration_max_attempts,
                 AccountMigrationItem.next_attempt_at.is_not(None),
                 AccountMigrationItem.next_attempt_at <= now_utc()),
        )

    async def _discover_page(self, run: AccountMigrationRun) -> int:
        if run.scan_phase == "done":
            return 0
        size = self.settings.account_migration_scan_page_size
        cursor = dict(run.scan_cursor or {})
        items = []
        if run.scan_phase == "works":
            namespace = cursor.get("kind", "illust")
            statement = select(PixivWork.id, PixivWork.work_type).where(
                PixivWork.work_kind == namespace,
            ).order_by(PixivWork.id).limit(size)
            if cursor.get("id"):
                statement = statement.where(PixivWork.id > str(cursor["id"]))
            rows = (await self.session.execute(statement)).all()
            items = [("novel" if kind == WorkType.NOVEL else "illust", str(wid)) for wid, kind in rows]
            if len(rows) < size:
                if namespace == "illust":
                    run.scan_cursor = {"kind": "novel", "id": ""}
                else:
                    run.scan_phase, run.scan_cursor = "series", {"type": "novel", "id": ""}
            else:
                run.scan_cursor = {"kind": namespace, "id": str(rows[-1][0])}
        elif run.scan_phase == "series":
            kind = cursor.get("type", "novel")
            statement = select(Series.id).where(Series.type == kind).order_by(Series.id).limit(size)
            if cursor.get("id"):
                statement = statement.where(Series.id > str(cursor["id"]))
            rows = (await self.session.execute(statement)).scalars().all()
            items = [(f"series_{kind}", str(sid)) for sid in rows]
            if len(rows) < size:
                if kind == "novel":
                    run.scan_cursor = {"type": "manga", "id": ""}
                else:
                    run.scan_phase, run.scan_cursor = "sources", {}
            else:
                run.scan_cursor = {"type": kind, "id": str(rows[-1])}
        elif run.scan_phase == "sources":
            statement = select(SyncSource.id, SyncSource.source_type, SyncSource.remote_user_id).where(
                SyncSource.source_type.in_((SyncSourceType.NOVEL_SERIES, SyncSourceType.MANGA_SERIES)),
            ).order_by(SyncSource.id).limit(size)
            if cursor.get("id"):
                statement = statement.where(SyncSource.id > int(cursor["id"]))
            rows = (await self.session.execute(statement)).all()
            items = [("series_novel" if kind == SyncSourceType.NOVEL_SERIES else "series_manga", str(sid))
                     for _, kind, sid in rows]
            if len(rows) < size:
                run.scan_phase, run.scan_cursor = "done", {}
            else:
                run.scan_cursor = {"id": int(rows[-1][0])}
        else:
            raise ValueError("Unknown account migration scan phase")
        await self._insert_items(run.id, items)
        run.updated_at = now_utc()
        await self.session.commit()
        return len(items)

    async def _insert_items(self, run_id: int, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        now = now_utc()
        values = [{"run_id": run_id, "kind": kind, "target_id": target,
                   "status": "pending", "attempts": 0, "next_attempt_at": None,
                   "last_error_type": None, "last_error": None,
                   "created_at": now, "updated_at": now, "finished_at": None}
                  for kind, target in sorted(set(items))]
        dialect = self.session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(AccountMigrationItem).values(values).on_conflict_do_nothing(
                index_elements=["run_id", "kind", "target_id"],
            )
        elif dialect in {"mysql", "mariadb"}:
            statement = mysql_insert(AccountMigrationItem).values(values)
            statement = statement.on_duplicate_key_update(target_id=statement.inserted.target_id)
        else:
            raise RuntimeError("Account migration requires MySQL or SQLite")
        await self.session.execute(statement)

    async def _process_item(self, run, item, *guards) -> bool:
        target = run.target_user_id
        try:
            # The client rechecks expected_user_id under its credential/write
            # lock immediately before POST; this cached check also stops a batch
            # promptly when credentials change between two items.
            await self._identity(force=False, expected=target)
        except PixivAuthError:
            await self._pause_runs(target_user_id=target, reason="Pixiv account or credentials changed")
            run.status = "paused"
            return False
        for guard in guards:
            guard.ensure_alive()
        item.status = "running"
        item.attempts += 1
        item.updated_at = now_utc()
        item.next_attempt_at = None
        await self.session.commit()
        try:
            if item.kind in {"illust", "novel"}:
                await self.client.ensure_bookmarked(
                    int(item.target_id), WorkType(item.kind), restrict="private", expected_user_id=target,
                )
            elif item.kind in {"series_novel", "series_manga"}:
                await self.client.ensure_series_followed(
                    item.kind.removeprefix("series_"), int(item.target_id), expected_user_id=target,
                )
            else:
                raise ValueError("Unknown migration item kind")
        except PixivAuthError:
            item.status = "pending"
            item.attempts = max(0, item.attempts - 1)
            item.last_error_type = "PixivAuthError"
            item.last_error = "Pixiv account or credentials changed"
            item.updated_at = now_utc()
            await self._pause_runs(target_user_id=target, reason=item.last_error)
            run.status = "paused"
            return False
        except (PixivNotFoundError, PixivRestrictedError, ValueError) as exc:
            item.status = "skipped"
            item.last_error_type = type(exc).__name__[:64]
            item.last_error = "Remote item is unavailable or cannot be migrated"
            item.finished_at = now_utc()
        except Exception as exc:
            item.status = "failed"
            item.last_error_type = type(exc).__name__[:64]
            # Exception bodies can include upstream credentials; persist only
            # their type and a safe explanation in the public migration API.
            item.last_error = "Remote migration failed; retry is scheduled when attempts remain"
            if item.attempts < self.settings.account_migration_max_attempts:
                delay = min(self.settings.account_migration_retry_max_seconds,
                            self.settings.account_migration_retry_base_seconds * 2 ** max(0, item.attempts - 1))
                item.next_attempt_at = now_utc() + timedelta(seconds=delay)
            else:
                item.finished_at = now_utc()
        else:
            item.status = "succeeded"
            item.last_error_type = None
            item.last_error = None
            item.finished_at = now_utc()
        item.updated_at = now_utc()
        for guard in guards:
            guard.ensure_alive()
        # Intentionally outside the remote-error handler. If COMMIT succeeds
        # but its response is lost, retain the durable running/succeeded state;
        # replay uses ensure_* and never overwrites an existing remote bookmark.
        await self.session.commit()
        return True

    async def _finish_if_done(self, run) -> None:
        if run.scan_phase != "done":
            return
        outstanding = (await self.session.execute(select(func.count()).select_from(AccountMigrationItem).where(
            AccountMigrationItem.run_id == run.id,
            or_(AccountMigrationItem.status.in_(("pending", "running")),
                and_(AccountMigrationItem.status == "failed",
                     AccountMigrationItem.attempts < self.settings.account_migration_max_attempts,
                     AccountMigrationItem.next_attempt_at.is_not(None))),
        ))).scalar_one()
        if not outstanding:
            run.status = "completed"
            run.finished_at = now_utc()
            run.updated_at = now_utc()
            await self.session.commit()

    async def get_run(self, run_id: int) -> dict:
        run = await self.session.get(AccountMigrationRun, run_id)
        if run is None:
            raise LookupError("Account migration run not found")
        groups = (await self.session.execute(select(
            AccountMigrationItem.status, func.count(),
        ).where(AccountMigrationItem.run_id == run_id).group_by(AccountMigrationItem.status))).all()
        counts = {status: 0 for status in ITEM_STATUSES}
        counts.update({status: int(count) for status, count in groups})
        return {
            "run_id": run.id, "target_user_id": run.target_user_id, "status": run.status,
            "automatic": run.automatic_key is not None, "scan_phase": run.scan_phase,
            "scan_complete": run.scan_phase == "done", "counts": counts, "total": sum(counts.values()),
            "created_at": run.created_at, "updated_at": run.updated_at,
            "finished_at": run.finished_at, "last_error": run.last_error,
        }

    async def retry_failed(self, run_id: int) -> dict:
        async with self._guard("coordinator") as guard:
            if guard is None:
                raise PixivUnavailableError("An account migration batch is already running")
            await self.session.rollback()
            target = await self._identity(force=True)
            run = await self.session.get(AccountMigrationRun, run_id)
            if run is None:
                raise LookupError("Account migration run not found")
            if run.target_user_id != target or not await self._activate(target, allow_account_switch=False):
                raise PixivAuthError("Migration belongs to a different Pixiv account")
            guard.ensure_alive()
            await self.session.execute(update(AccountMigrationItem).where(
                AccountMigrationItem.run_id == run_id, AccountMigrationItem.status == "failed",
            ).values(status="pending", attempts=0, next_attempt_at=None, last_error_type=None,
                     last_error=None, finished_at=None, updated_at=now_utc()))
            run.status = "pending"
            run.finished_at = None
            run.last_error = None
            run.updated_at = now_utc()
            await self.session.commit()
            return await self.get_run(run_id)
