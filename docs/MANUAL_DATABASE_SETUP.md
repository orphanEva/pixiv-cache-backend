# pixiv_archive: manual MySQL initialization

**New, empty MySQL 8.0+ database only.** This project does not create any
production database, table, or migration record at runtime.

1. Review [sql/pixiv_archive.sql](../sql/pixiv_archive.sql). It is the new
   canonical DDL based on the supplied seven-table design with additions
   needed for reliable immutable version history.
2. Open your MySQL administrator client (Navicat/DataGrip/mysql CLI),
   and manually run the SQL **once on a new database**.
3. Create your own restricted application user. For example, update the
   password and allowed host below:

```sql
CREATE USER 'pixiv'@'YOUR_API_HOST' IDENTIFIED BY 'YOUR_OWN_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE ON pixiv_archive.* TO 'pixiv'@'YOUR_API_HOST';
```

4. Set `DATABASE_URL` in the private `.env`, e.g.
   `mysql+asyncmy://pixiv:PASSWORD@MYSQL_HOST:3306/pixiv_archive?charset=utf8mb4`.
   URL-encode reserved password characters; do not commit your `.env`.
5. Supply a private Pixiv refresh token and an API key of at least 32
   random characters; start with `docker compose up -d --build`.
6. Verify `curl http://127.0.0.1:18081/health/ready` reports
   MySQL and Redis connectivity.

## Existing installations or imported data

Do NOT execute the new-install script over an existing
`pixiv_archive` or `pixiv_cache` database that contains data, and
do not execute the original dump with `DROP TABLE IF EXISTS`.
The original dump had no reliable `version_token`, could require a
nonexistent `remote_update_at`, and recorded only prior versions in
`work_history`. This application requires **all** version records,
including the current one, with registered media paths. Migrating
existing rows and files requires a separately reviewed and verified
data/file migration, with a full backup and rollback plan.

The API account needs only DML privileges. Production containers do
not execute Alembic or run `CREATE TABLE`. CI initialization is
intentionally restricted to the disposable GitHub Actions MySQL.
