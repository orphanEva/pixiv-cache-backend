from pathlib import Path
import re

from app.core.schema_version import EXPECTED_SCHEMA_VERSION
from app.models.work import Base


def test_archive_schema_contract():
    expected = {
        "schema_version",
        "works",
        "series",
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
    for file_type in ("ugoira_zip", "ugoira_meta", "ugoira_mp4"):
        assert f"'{file_type}'" in sql


def test_manual_v1_to_v2_upgrade_exists():
    sql = Path("sql/upgrades/v1_to_v2.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS schema_version" in sql
    assert "DROP TABLE" not in sql
    assert "ON DUPLICATE KEY UPDATE" in sql


def test_manual_v2_to_v3_upgrade_exists():
    sql = Path("sql/upgrades/v2_to_v3.sql").read_text()
    assert "CREATE TABLE sync_sources" in sql
    assert "SET version = 3" in sql
    assert "DROP TABLE" not in sql


def test_manual_v3_to_v4_upgrade_exists():
    sql = Path("sql/upgrades/v3_to_v4.sql").read_text()
    assert "CREATE TABLE work_sources" in sql
    assert "SET version = 4" in sql
    assert "DROP TABLE" not in sql


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
