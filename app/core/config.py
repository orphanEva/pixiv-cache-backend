from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "pixiv-cache-backend"
    database_url: str = "mysql+asyncmy://pixiv:pixiv@localhost:3306/pixiv_archive?charset=utf8mb4"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: Path = Path("./data/pixiv")
    pixiv_refresh_token: str = ""
    api_key: str = ""
    remote_check_ttl_seconds: int = 0
    lock_ttl_seconds: int = 180
    lock_wait_seconds: int = 30
    serve_stale_on_remote_unavailable: bool = True
    max_asset_bytes: int = 150 * 1024 * 1024
    download_timeout_seconds: int = 120
    ugoira_generate_mp4: bool = True
    ugoira_max_frames: int = 10000
    ugoira_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    ffmpeg_binary: str = "ffmpeg"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
