# Archive worker

## Why it exists

Large Pixiv works can spend most of their time downloading, validating ZIP
members, extracting frames and running FFmpeg. These operations no longer need
to hold an HTTP request open.

The deployment now contains:

```text
api
  -> enqueue Redis Stream job

redis
  -> durable queue + job status + worker heartbeat

worker
  -> Pixiv fetch
  -> immutable local storage
  -> MySQL metadata/version transaction
```

MySQL is still external and manually initialized.

## Delivery semantics

The queue is at-least-once. Redis consumer-group pending messages survive worker
failure. Another worker may reclaim a message after `ARCHIVE_JOB_CLAIM_IDLE_MS`.

While a job is actually running, its worker refreshes the pending-message idle
timer. CacheService's renewable per-work Redis lock and immutable version token
make a reclaimed/repeated job safe: it cannot silently commit two writers for
the same work.

Completed job hashes are retained for
`ARCHIVE_JOB_STATUS_TTL_SECONDS` (default 7 days).

## Graceful shutdown

The Compose worker has a 30-minute stop grace period. SIGTERM asks the worker to
finish its current job, ACK it, then exit instead of killing FFmpeg/downloads
mid-write.

## Failure/retry

The worker records a sanitized error type/message and marks the job `failed`.
It intentionally does not hammer Pixiv with automatic rapid retries. Retry is
explicit:

```http
POST /api/jobs/{job_id}/retry
```

## Scaling

Multiple workers can consume the same Redis consumer group. Shared storage must
then truly be shared between those worker containers/hosts. On the default
single-host Compose deployment, `./data:/data/pixiv` satisfies that condition.
