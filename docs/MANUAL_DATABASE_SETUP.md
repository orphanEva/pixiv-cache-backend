# pixiv_archive: manual MySQL initialization

**New, empty MySQL 8.0+ database only.** This project does not create any
production database, table, or migration record at runtime.

1. Review [sql/pixiv_archive.sql](../sql/pixiv_archive.sql). It is the new
   canonical DDL based on the supplied seven archive tables plus the
   `schema_version` metadata table used to verify manual upgrades.
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
6. Verify `curl http://127.0.0.1:18081/health/ready` reports MySQL, schema and
   Redis readiness.

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


## Manual schema upgrades

Runtime containers never execute DDL. If an existing archive was initialized
from the pre-v1.1 DDL (seven archive tables but no `schema_version` table),
apply this once with an administrator account:

```text
sql/upgrades/v1_to_v2.sql
```

After that, the runtime DML-only user only needs to read
`schema_version`. Application startup checks that the database version equals
the version required by the running code and refuses to start on mismatch.


### v2 -> v3

v1.4 adds the persistent `sync_sources` table. Existing schema-v2 databases
must apply:

```text
sql/upgrades/v2_to_v3.sql
```

After the script succeeds, `schema_version.id=1` must contain `version=3`.
For a database that predates schema versioning, apply `v1_to_v2.sql` first,
then `v2_to_v3.sql`. Runtime containers never execute either script.


### v3 -> v4

v1.5 adds `work_sources` so one archived work can retain every synchronization
source that discovered it.

Existing schema-v3 databases must apply:

```text
sql/upgrades/v3_to_v4.sql
```

After the script succeeds, `schema_version.id=1` must contain `version=4`.
For older installations, apply the upgrade scripts in order. Runtime containers
never execute these scripts automatically.
