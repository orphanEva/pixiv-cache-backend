# Ugoira archival

Pixiv Ugoira is archived as source material plus a derived preview.

## Source of truth

- `original.zip`: Pixiv Ugoira ZIP returned by `ugoira_metadata`
- `ugoira_meta.json`: ordered frame names and exact delay in milliseconds
- `frames/`: validated extracted frame sequence

The backend verifies that every frame declared by Pixiv exists in the ZIP.
Corrupt ZIP members, missing frames, unsupported frame extensions, excessive
frame counts, and excessive uncompressed size cause the new version to fail
before it is committed to MySQL.

## Derived MP4

When `UGOIRA_GENERATE_MP4=true`, FFmpeg's concat demuxer is fed the exact
per-frame duration. Output is VFR H.264-compatible MP4 with yuv420p pixel
format and even dimensions. If FFmpeg fails, the valid raw archive remains
usable and the database simply has no `ugoira_mp4` asset for that version.

The Docker image installs FFmpeg. If running outside Docker, ensure the
`FFMPEG_BINARY` command exists.

## File types

The manually maintained DDL contains explicit enums for:

- `ugoira_zip`
- `ugoira_meta`
- `ugoira_mp4`
- `cover`

Because production does not run migrations automatically, initialize a new
empty database from the current `sql/pixiv_archive.sql`. If the database was
already initialized with an older DDL, apply a reviewed manual ALTER instead
of recreating tables.
