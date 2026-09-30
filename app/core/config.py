from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "pixiv-cache-backend"
    database_url: str = "mysql+asyncmy://pixiv:pixiv@localhost:3306/pixiv_archive?charset=utf8mb4"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: Path = Path("./data/pixiv")

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
    lock_ttl_seconds: int = 180
    lock_wait_seconds: int = 30
    serve_stale_on_remote_unavailable: bool = True
    max_asset_bytes: int = 150 * 1024 * 1024
    download_timeout_seconds: int = 120
    ugoira_generate_mp4: bool = True
    ugoira_prefer_original: bool = True
    ugoira_max_frames: int = 10000
    ugoira_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    ffmpeg_binary: str = "ffmpeg"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
