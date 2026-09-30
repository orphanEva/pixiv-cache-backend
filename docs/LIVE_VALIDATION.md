# Real Pixiv integration checklist

These instructions run on **your own server**, not in GitHub Actions. Never paste
`PIXIV_REFRESH_TOKEN`, `API_KEY`, a complete `.env`, or authorization headers
into issues or chat.

1. Clone the repository, then copy `.env.example` to `.env`.
2. Generate three separate random secrets. For example,
   `python3 -c 'import secrets; print(secrets.token_urlsafe(36))'`.
   Set `API_KEY`, `MYSQL_PASSWORD`, and `MYSQL_ROOT_PASSWORD`; update
   `DATABASE_URL` to use the same `MYSQL_PASSWORD`. Avoid punctuation that
   requires URL encoding in `DATABASE_URL` or URL-encode that password.
3. On your local machine, provide your **own authorized** Pixiv refresh token as
   `PIXIV_REFRESH_TOKEN`. Never commit it.
4. Run `docker compose config --quiet`, then
   `docker compose up -d --build`.
5. Run `curl -sS http://127.0.0.1:18081/health/ready`; expected:
   `{"status":"ok","checks":{"mysql":true,"redis":true}}`.
6. Using a real public illustration ID that your account can view,
   run `curl -sS -H "X-API-Key: YOUR_LOCAL_KEY" 'http://127.0.0.1:18081/api/illust/REAL_ID'`.
   For a real public novel ID run the corresponding `/api/novel/REAL_ID`.
   Replace placeholders locally; avoid placing credentials in shell history
   where possible.
7. Repeat the same request. Compare `source` and `version`.
   With no remote changes the second call should say `local-validated`.
   `?refresh=false` should return `local` without contacting Pixiv.
8. If a call fails, share *redacted* status codes and the last lines from
   `docker compose logs --tail=100 api`. Do not share environment variables,
   tokens, cookies, signed image URLs, or author-private content.

Only APIs and database/caching behavior have been tested in CI. Real Pixiv
authentication and content downloads require this separate integration test.
The metadata fingerprint cannot guarantee detection of a remote image whose
binary content changed while its remote URL and metadata stayed identical.
