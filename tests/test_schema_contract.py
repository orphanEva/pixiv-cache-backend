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
    assert "ENUM('author','bookmarks','watchlist')" in sql
    assert "MODIFY COLUMN type ENUM('author','bookmarks','watchlist')" in Path("sql/upgrades/v6_to_v7.sql").read_text()
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


def test_app_never_runs_schema_mutations_on_start():
    dockerfile = Path("Dockerfile").read_text()
    assert "alembic upgrade" not in dockerfile
    assert "pixiv_archive.sql" not in dockerfile
    compose = Path("docker-compose.yml").read_text()
    assert "image: mysql:" not in compose


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
