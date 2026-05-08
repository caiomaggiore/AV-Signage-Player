from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, field_validator


class NetworkConfig(BaseModel):
    mode: str = "dhcp"
    ip: str = ""
    mask: str = ""
    gateway: str = ""
    dns: List[str] = []

    @field_validator("mode")
    @classmethod
    def mode_must_be_valid(cls, v: str) -> str:
        if v not in ("dhcp", "static"):
            raise ValueError("mode deve ser 'dhcp' ou 'static'")
        return v


class ServerConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class MediaConfig(BaseModel):
    local_path: str = "/opt/av-signage/media/local"
    server_path: str = "/opt/av-signage/media/server"
    cache_path: str = "/opt/av-signage/media/cache"
    download_path: str = "/opt/av-signage/media/downloading"
    allowed_extensions: List[str] = [".mp4", ".mov", ".mkv"]
    max_upload_mb: int = 2048


class PlayerConfig(BaseModel):
    engine: str = "mpv"
    loop_default: bool = True
    fullscreen: bool = True


class AuthConfig(BaseModel):
    session_timeout_minutes: int = 60
    max_login_attempts: int = 5
    lockout_minutes: int = 15


class DefaultsConfig(BaseModel):
    product_name: str = "AV Signage Player"
    default_display_name: str = "Unconfigured Signage Player"
    default_device_name: str = "maggiore"
    hostname_prefix: str = "signage"
    default_hostname: str = "signage-maggiore"
    local_web_port: int = 8080
    network: NetworkConfig = NetworkConfig()
    server: ServerConfig = ServerConfig()
    media: MediaConfig = MediaConfig()
    player: PlayerConfig = PlayerConfig()
    auth: AuthConfig = AuthConfig()


class UserConfig(BaseModel):
    display_name: str = ""
    device_name: str = ""
    hostname: str = ""
    network: NetworkConfig = NetworkConfig()
    server: ServerConfig = ServerConfig()
    manual_mode: bool = False
    last_media: str = ""


class MergedConfig(BaseModel):
    """Configuração final mesclada: defaults + user_config."""
    product_name: str
    display_name: str
    device_name: str
    hostname: str
    hostname_prefix: str
    local_web_port: int
    network: NetworkConfig
    server: ServerConfig
    media: MediaConfig
    player: PlayerConfig
    auth: AuthConfig
    manual_mode: bool
    last_media: str


class IdentityConfig(BaseModel):
    device_id: str = ""
    hardware_model: str = ""
    software_version: str = "0.1.0"
    created_at: str = ""


class PairingConfig(BaseModel):
    paired: bool = False
    pairing_code: str = ""
    server_url: str = ""
    device_token: str = ""
    last_pairing_attempt: str = ""
