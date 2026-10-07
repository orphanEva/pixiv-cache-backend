from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "pixiv-cache-backend"
    database_url: str = "mysql+asyncmy://pixiv:pixiv@localhost:3306/pixiv_archive?charset=utf8mb4"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: Path = Path("./data/pixiv")
    schema_check_on_startup: bool = True

    # Manual credentials remain supported. AUTO_AUTH can populate missing/expired
    # derived credentials from PIXIV_USERNAME/PIXIV_PASSWORD instead.
    pixiv_refresh_token: str = ""
    pixiv_cookie: str = ""
    pixiv_auto_auth: bool = False
    pixiv_username: str = ""
    pixiv_password: str = ""
    pixiv_totp_secret: str = ""
    pixiv_auth_cache_dir: Path = Path("/data/pixiv/auth")
    pixiv_auto_login_cooldown_seconds: int = 900
    pixiv_browser_timeout_seconds: int = 45
    pixiv_web_user_agent: str = "Mozilla/5.0"
    pixiv_web_accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8"
    pixiv_web_locale: str = "zh-CN"

    api_key: str = ""
    remote_check_ttl_seconds: int = 0
    pixiv_deep_image_check: bool = False
    cache_lock_prefix: str = "pixiv-cache:"
    lock_ttl_seconds: int = 180
    lock_wait_seconds: int = 30
    serve_stale_on_remote_unavailable: bool = True
    max_asset_bytes: int = 150 * 1024 * 1024
    storage_audit_max_findings: int = 200
    storage_min_free_bytes: int = 2 * 1024 * 1024 * 1024
    storage_part_stale_seconds: int = 24 * 3600
    storage_orphan_grace_seconds: int = 24 * 3600
    download_timeout_seconds: int = 120
    ugoira_generate_mp4: bool = True
    ugoira_keep_extracted_frames: bool = False
    ugoira_prefer_original: bool = True
    ugoira_max_frames: int = 10000
    ugoira_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    ffmpeg_binary: str = "ffmpeg"

    archive_job_stream: str = "pixiv:archive:jobs"
    archive_job_group: str = "pixiv-archive-workers"
    archive_job_status_prefix: str = "pixiv:archive:job:"
    archive_job_dedupe_prefix: str = "pixiv:archive:dedupe:"
    archive_job_status_ttl_seconds: int = 7 * 24 * 3600
    archive_job_dedupe_ttl_seconds: int = 6 * 3600
    archive_job_claim_idle_ms: int = 15 * 60 * 1000
    archive_job_retry_zset: str = "pixiv:archive:retry"
    archive_job_index_zset: str = "pixiv:archive:index"
    archive_job_status_index_prefix: str = "pixiv:archive:status:"
    archive_job_dead_stream: str = "pixiv:archive:dead"
    archive_job_dead_maxlen: int = 10000
    archive_job_max_attempts: int = 5
    archive_job_retry_base_seconds: int = 30
    archive_job_retry_max_seconds: int = 30 * 60
    archive_worker_block_ms: int = 5000
    archive_worker_heartbeat_key: str = "pixiv:archive:worker:healthy"
    archive_worker_heartbeat_ttl_seconds: int = 30

    sync_job_stream: str = "pixiv:sync:jobs"
    sync_job_group: str = "pixiv-sync-workers"
    sync_job_status_prefix: str = "pixiv:sync:job:"
    sync_job_dedupe_prefix: str = "pixiv:sync:dedupe:"
    sync_job_status_ttl_seconds: int = 7 * 24 * 3600
    sync_job_dedupe_ttl_seconds: int = 6 * 3600
    sync_job_claim_idle_ms: int = 15 * 60 * 1000
    sync_worker_block_ms: int = 5000
    sync_worker_heartbeat_key: str = "pixiv:sync:worker:healthy"
    sync_worker_heartbeat_ttl_seconds: int = 30
    sync_scheduler_poll_seconds: int = 30
    sync_due_batch_size: int = 20
    sync_schedule_reservation_seconds: int = 15 * 60
    sync_failure_retry_seconds: int = 15 * 60
    sync_max_pages_per_run: int = 1000
    sync_frontier_size: int = 50
    sync_page_delay_seconds: float = 0.2

    maintenance_freeze_key: str = "pixiv:maintenance:freeze"
    maintenance_freeze_ttl_seconds: int = 2 * 3600
    maintenance_freeze_wait_seconds: int = 5
    maintenance_wait_for_writers_seconds: int = 30 * 60
    maintenance_database_url: str = ""
    backup_root: Path = Path("/data/pixiv/.backups")

    integrity_audit_interval_seconds: int = 6 * 3600
    integrity_audit_initial_delay_seconds: int = 60
    integrity_audit_verify_hash: bool = False
    integrity_audit_repair_safe: bool = False
    integrity_worker_lock_key: str = "pixiv:maintenance:integrity-lock"
    integrity_worker_lock_ttl_seconds: int = 30 * 60
    integrity_worker_heartbeat_key: str = "pixiv:integrity:worker:healthy"
    integrity_worker_heartbeat_ttl_seconds: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
