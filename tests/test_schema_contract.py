from pathlib import Path
import re

from app.core.schema_version import EXPECTED_SCHEMA_VERSION
from app.models.work import Base


def test_archive_schema_contract():
    expected = {
        "schema_version",
        "works",
        "series",
        "series_members",
        "tags",
        "work_files",
        "work_history",
        "work_history_files",
        "work_tag_relation",
        "sync_sources",
        "work_sources",
        "account_migration_state",
        "account_migration_runs",
        "account_migration_items",
    }
    assert set(Base.metadata.tables) == expected
    sql = Path("sql/pixiv_archive.sql").read_text()
    for table in expected:
        assert "CREATE TABLE " + table in sql
    assert "DROP TABLE" not in "\n".join(
        line for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "remote_update_at DATETIME NULL" in sql
    assert f"VALUES (1, {EXPECTED_SCHEMA_VERSION})" in sql
    for file_type in ("ugoira_zip", "ugoira_meta", "ugoira_mp4"):
        assert f"'{file_type}'" in sql


def test_typed_series_composite_keys():
    from app.models.work import Series, SeriesMember, PixivWork
    assert set(Series.__table__.primary_key.columns.keys()) == {"id", "type"}
    assert set(SeriesMember.__table__.primary_key.columns.keys()) == {"series_type", "series_id", "work_id"}
    fks = {tuple(fk.column_keys) for fk in PixivWork.__table__.foreign_key_constraints}
    assert ("series_type", "series_id") in fks
    sql = Path("sql/pixiv_archive.sql").read_text()
    assert "PRIMARY KEY (type, id)" in sql
    assert "PRIMARY KEY (series_type,series_id,work_id)" in sql
    assert "FOREIGN KEY (series_type,series_id) REFERENCES series(type,id)" in sql


def test_v7_persistent_sync_state_and_series_sources():
    from app.models.work import SyncSource, SyncSourceType
    expected = {"author", "bookmarks", "novel_series", "manga_series", "followed_series", "watchlist"}
    assert {item.value for item in SyncSourceType} == expected
    assert {"expand_series", "scan_state_json"} <= set(SyncSource.__table__.columns.keys())
    sql = Path("sql/pixiv_archive.sql").read_text(encoding="utf-8")
    migration = Path("sql/upgrades/v6_to_v7.sql").read_text(encoding="utf-8")
    for value in expected:
        assert f"'{value}'" in sql
        assert f"'{value}'" in migration
    assert "ADD COLUMN expand_series" in migration
    assert "ADD COLUMN scan_state_json" in migration
    assert "WHERE id=1 AND version=6" in migration
    assert "SIGNAL SQLSTATE '45000'" in migration


def test_legacy_watchlist_rows_remain_readable_and_all_sql_paths_preserve_types():
    from sqlalchemy.dialects import sqlite
    from app.models.work import SyncSource, SyncSourceType

    enum_type = SyncSource.__table__.columns["type"].type
    decode = enum_type.result_processor(sqlite.dialect(), None)
    assert decode("watchlist") is SyncSourceType.WATCHLIST
    assert decode("followed_series") is SyncSourceType.FOLLOWED_SERIES
    expected = {item.value for item in SyncSourceType}
    for filename in (
        "sql/pixiv_archive.sql", "sql/upgrades/v6_to_v7.sql",
        "sql/upgrades/v7_to_v8.sql", "sql/upgrades/v8_to_v9.sql",
        "sql/upgrades/v7_v9_sync_sources_compat.sql",
    ):
        sql = Path(filename).read_text(encoding="utf-8")
        declarations = re.findall(r"\btype ENUM\(([^)]+)\)", sql)
        source_types = [set(re.findall(r"'([^']+)'", declaration))
                        for declaration in declarations if "'author'" in declaration]
        assert source_types == [expected], filename


def test_both_v7_branches_have_a_safe_upgrade_path():
    migration = Path("sql/upgrades/v7_to_v8.sql").read_text(encoding="utf-8")
    for column in ("expand_series", "scan_state_json"):
        guard = rf"IF NOT EXISTS \(SELECT 1 FROM information_schema\.COLUMNS\s+WHERE TABLE_SCHEMA=DATABASE\(\) AND TABLE_NAME='sync_sources' AND COLUMN_NAME='{column}'\) THEN\s+ALTER TABLE sync_sources ADD COLUMN {column}\b.*?END IF;"
        assert re.search(guard, migration, re.S), column
    assert migration.index("CALL pixiv_require_schema_v7()") < migration.index("ADD COLUMN account_id")
    patch = Path("sql/upgrades/v7_v9_sync_sources_compat.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in patch.splitlines() if not line.lstrip().startswith("--"))
    assert "version IN (7,8,9)" in statements
    assert "CAST(type AS CHAR) NOT IN" in statements
    assert "UPDATE schema_version" not in statements
    assert "UPDATE sync_sources" not in statements
    assert "DELETE FROM" not in statements
    assert "DROP TABLE" not in statements
    assert "USE pixiv_archive" not in statements


def test_app_never_runs_schema_mutations_on_start():
    dockerfile = Path("Dockerfile").read_text()
    assert "alembic upgrade" not in dockerfile
    assert "pixiv_archive.sql" not in dockerfile
    compose = Path("docker-compose.yml").read_text()
    assert "image: mysql:" not in compose


def test_v8_account_progress_schema():
    from app.models.work import SyncSource, AccountMigrationItem
    assert "account_id" in SyncSource.__table__.columns
    assert set(AccountMigrationItem.__table__.primary_key.columns.keys()) == {
        "run_id", "kind", "target_id",
    }
    migration = Path("sql/upgrades/v7_to_v8.sql").read_text(encoding="utf-8")
    assert "WHERE id=1 AND version=7" in migration
    assert "SIGNAL SQLSTATE '45000'" in migration
    assert "ADD COLUMN account_id" in migration
    for name in ("state", "runs", "items"):
        assert "CREATE TABLE account_migration_" + name in migration


def test_v9_namespace_keys_and_manual_backfill():
    from app.models.work import PixivWork, PixivVersion, CurrentFile, WorkTagRelation, WorkSource
    assert tuple(PixivWork.__table__.primary_key.columns.keys()) == ("work_kind", "id")
    for model in (PixivVersion, CurrentFile, WorkTagRelation, WorkSource):
        parents = [fk for fk in model.__table__.foreign_key_constraints if fk.referred_table.name == "works"]
        assert len(parents) == 1
        assert tuple(parents[0].column_keys) == ("work_kind", "work_id")
    assert "next_recheck_at" in PixivWork.__table__.columns
    migration = Path("sql/upgrades/v8_to_v9.sql").read_text(encoding="utf-8")
    assert "WHERE id=1 AND version=8" in migration
    assert "SIGNAL SQLSTATE '45000'" in migration
    assert "UPDATE works SET work_kind=IF(type='novel','novel','illust')" in migration
    assert "REFERENCES works(work_kind,id)" in migration
    ddl = Path("sql/pixiv_archive.sql").read_text(encoding="utf-8")
    assert "REFERENCES works(id)" not in ddl


def test_compose_contains_separate_archive_worker():
    compose = Path("docker-compose.yml").read_text()
    assert "worker:" in compose
    assert 'python", "-m", "app.workers.archive_worker' in compose
    assert "stop_grace_period: 30m" in compose


def test_compose_contains_separate_sync_worker():
    compose = Path("docker-compose.yml").read_text()
    assert "sync:" in compose
    assert 'python", "-m", "app.workers.sync_worker' in compose
    assert "SYNC_WORKER_HEARTBEAT_KEY" in compose


def test_compose_contains_separate_maintenance_worker():
    compose = Path("docker-compose.yml").read_text()
    assert "maintenance:" in compose
    assert 'python", "-m", "app.workers.integrity_worker' in compose
    assert "WORKER_HEARTBEAT_KEY" in compose



def test_current_ddl_has_chinese_comments_for_every_table_and_column():
    sql = Path("sql/pixiv_archive.sql").read_text(encoding="utf-8")
    blocks = re.findall(
        r"CREATE TABLE\s+(\w+)\s*\((.*?)\) ENGINE=InnoDB[^;]*COMMENT='([^']+)'",
        sql,
        flags=re.S,
    )
    assert len(blocks) == len(Base.metadata.tables)

    for table_name, body, table_comment in blocks:
        assert any("\u4e00" <= ch <= "\u9fff" for ch in table_comment), table_name
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            upper = line.upper()
            if upper.startswith(
                ("PRIMARY KEY", "KEY ", "UNIQUE KEY", "CONSTRAINT", "CHECK ")
            ):
                continue
            assert " COMMENT '" in line, f"{table_name}: {line}"
            comment = line.split(" COMMENT '", 1)[1].rsplit("'", 1)[0]
            assert any("\u4e00" <= ch <= "\u9fff" for ch in comment), (
                f"{table_name}: {line}"
            )
