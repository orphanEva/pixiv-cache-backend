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
    archive_worker_block_ms: int = 5000
    archive_worker_heartbeat_key: str = "pixiv:archive:worker:healthy"
    archive_worker_heartbeat_ttl_seconds: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
