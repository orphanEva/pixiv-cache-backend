"""Manually execute the project's DDL in a DISPOSABLE GitHub Actions MySQL.
This script must NOT be called by the application's startup or production Docker.
"""
import asyncio
import os
from pathlib import Path

import asyncmy


async def main():
    if not os.getenv("GITHUB_ACTIONS") or not os.getenv("CI_MYSQL_ROOT_PASSWORD"):
        raise SystemExit("Refusing DDL outside disposable GitHub Actions database")
    sql = Path("sql/pixiv_archive.sql").read_text(encoding="utf-8")
    connection = await asyncmy.connect(
        host="127.0.0.1", port=3306,
        user="root", password=os.environ["CI_MYSQL_ROOT_PASSWORD"],
        db="mysql", autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            # The checked-in schema consists only of plain DDL statements;
            # comments cannot contain semicolons. Never parse arbitrary dumps.
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    await cursor.execute(statement)
        print("Disposable pixiv_archive schema initialized from checked-in DDL")
    finally:
        connection.close()


if __name__ == "__main__":
    asyncio.run(main())
