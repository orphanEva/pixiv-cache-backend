from pathlib import Path

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
