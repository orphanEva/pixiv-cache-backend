"""MCP-only configuration, deliberately independent of backend DB credentials."""
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIXIV_MCP_", env_file=None, extra="ignore", hide_input_in_errors=True,
        frozen=True,
    )

    backend_url: str = "http://127.0.0.1:18081"
    api_key: SecretStr
    timeout_seconds: float = Field(default=60, ge=1, le=600)
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    read_only: bool = False

    @field_validator("backend_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("backend_url must be an absolute HTTP(S) URL") from None
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in value or "#" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "\\" in value or "%" in parts.netloc
            or any(part in {".", ".."} for part in parts.path.split("/"))
            or "%" in parts.path or (port is not None and port < 1)
        ):
            raise ValueError("backend_url must be HTTP(S), without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if len(key) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("api_key must contain at least 32 printable ASCII characters without spaces")
        return value
