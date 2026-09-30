from pathlib import Path
from app.models.work import Base


def test_seven_table_contract():
    expected = {"works", "series", "tags", "work_files", "work_history", "work_history_files", "work_tag_relation"}
    assert set(Base.metadata.tables) == expected
    sql = Path("sql/pixiv_archive.sql").read_text()
    for table in expected:
        assert "CREATE TABLE " + table in sql
    assert "DROP TABLE" not in "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "remote_update_at DATETIME NULL" in sql


def test_app_never_runs_migrations_on_start():
    dockerfile = Path("Dockerfile").read_text()
    assert "alembic upgrade" not in dockerfile
    compose = Path("docker-compose.yml").read_text()
    assert "image: mysql:" not in compose
