# External MySQL: manual initialization only

The application **never creates databases or tables** on startup.
Docker Compose starts only the API and Redis; supply your own MySQL 8.0+ instance.
The schema is in [../sql/mysql-schema.sql](../sql/mysql-schema.sql).

## 1. Initialize MySQL manually

Connect to your MySQL instance as an authorized database administrator,
open `sql/mysql-schema.sql` in Navicat/DataGrip, and run the full script.
It creates the `pixiv_cache` database and its three application tables.
Run it **once** on a new instance; back up existing data before applying
future manual schema changes.

Create a dedicated `pixiv` database user yourself and grant it the minimum
needed runtime permissions (`SELECT`, `INSERT`, `UPDATE`, `DELETE` on
`pixiv_cache.*`). Do not give the API user `CREATE`, `ALTER`, `DROP`
or other schema-migration permissions.

SQL example (replace credentials before running, do not commit passwords):

```sql
CREATE USER 'pixiv'@'%' IDENTIFIED BY 'YOUR_OWN_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE ON pixiv_cache.* TO 'pixiv'@'%';
```

Restrict allowed hosts to your real deployment network whenever possible
rather than using `%`. Ensure that MySQL accepts connections from the
API container and is protected by a firewall.

## 2. Configure and start

```sh
cp .env.example .env
# Edit DATABASE_URL to point to your already-created database.
# Set API_KEY (>= 32 random characters) and PIXIV_REFRESH_TOKEN locally.
docker compose config --quiet
docker compose up -d --build
curl -sS http://127.0.0.1:18081/health/ready
```

Expected: `{"status":"ok","checks":{"mysql":true,"redis":true}}`.
If the schema is missing, requests depending on its tables will fail: the
application will NOT try to create or repair it automatically.

## Notes

- Do not execute `alembic upgrade head` on deployed systems.
- CI may use an ephemeral throwaway database to test Alembic migration
  correctness; that is separate from your deployed database initialization.
- On an existing populated database, inspect the DDL and migrate carefully
  rather than running a `CREATE` script blindly.
