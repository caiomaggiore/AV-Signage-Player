from __future__ import annotations

import json
import logging
import random
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models import (
    DefaultsConfig,
    IdentityConfig,
    MergedConfig,
    NetworkConfig,
    PairingConfig,
    UserConfig,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path("/opt/av-signage")
CONFIG_DIR = BASE_DIR / "config"

DEFAULTS_FILE = CONFIG_DIR / "defaults.json"
USER_CONFIG_FILE = CONFIG_DIR / "user_config.json"
PENDING_CONFIG_FILE = CONFIG_DIR / "pending_config.json"
IDENTITY_FILE = CONFIG_DIR / "identity.json"
PAIRING_FILE = CONFIG_DIR / "pairing.json"


def _load_json(path: Path) -> dict:
    """Carrega JSON de um arquivo, retorna {} em caso de erro."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Arquivo não encontrado: %s", path)
        return {}
    except json.JSONDecodeError as e:
        logger.error("JSON inválido em %s: %s", path, e)
        return {}


def _save_json(path: Path, data: dict) -> None:
    """Salva dicionário como JSON com indentação."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("Salvo: %s", path)


def _get_hardware_model() -> str:
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00").strip()
        return model
    except Exception:
        return "Unknown"


def _get_device_id() -> str:
    try:
        serial = Path("/proc/device-tree/serial-number").read_text().strip("\x00").strip()
        return f"RPi-{serial[:8].upper()}"
    except Exception:
        try:
            hostname = socket.gethostname()
            return f"RPi-{hostname}"
        except Exception:
            return f"RPi-{random.randint(10000000, 99999999)}"


def _generate_pairing_code() -> str:
    """Gera código de pareamento no formato NNN-NNN."""
    return f"{random.randint(100, 999)}-{random.randint(100, 999)}"


class ConfigService:
    def __init__(self) -> None:
        self._defaults: Optional[DefaultsConfig] = None
        self._user: Optional[UserConfig] = None
        self._merged: Optional[MergedConfig] = None
        self._identity: Optional[IdentityConfig] = None
        self._pairing: Optional[PairingConfig] = None

    def load(self) -> None:
        """Carrega todos os arquivos de configuração e mescla."""
        self._defaults = DefaultsConfig(**_load_json(DEFAULTS_FILE))
        user_data = _load_json(USER_CONFIG_FILE)
        self._user = UserConfig(**user_data)
        self._merged = self._merge()
        self._identity = self._load_identity()
        self._pairing = self._load_pairing()
        logger.info("Configuração carregada. Hostname: %s", self._merged.hostname)

    def _merge(self) -> MergedConfig:
        d = self._defaults
        u = self._user
        return MergedConfig(
            product_name=d.product_name,
            display_name=u.display_name or d.default_display_name,
            device_name=u.device_name or d.default_device_name,
            hostname=u.hostname or d.default_hostname,
            hostname_prefix=d.hostname_prefix,
            local_web_port=d.local_web_port,
            network=u.network if u.network.mode != "dhcp" or u.network.ip else d.network,
            server=u.server if u.server.enabled else d.server,
            media=d.media,
            player=u.player if u.player is not None else d.player,
            auth=d.auth,
            manual_mode=u.manual_mode,
            last_media=u.last_media,
        )

    def _load_identity(self) -> IdentityConfig:
        data = _load_json(IDENTITY_FILE)
        identity = IdentityConfig(**data)

        if not identity.device_id or not identity.hardware_model:
            identity.device_id = _get_device_id()
            identity.hardware_model = _get_hardware_model()
            identity.created_at = datetime.now(timezone.utc).isoformat()
            _save_json(IDENTITY_FILE, identity.model_dump())
            logger.info("Identidade gerada: %s", identity.device_id)

        return identity

    def _load_pairing(self) -> PairingConfig:
        data = _load_json(PAIRING_FILE)
        pairing = PairingConfig(**data)

        if not pairing.pairing_code:
            pairing.pairing_code = _generate_pairing_code()
            _save_json(PAIRING_FILE, pairing.model_dump())
            logger.info("Código de pareamento gerado: %s", pairing.pairing_code)

        return pairing

    @property
    def config(self) -> MergedConfig:
        if self._merged is None:
            raise RuntimeError("ConfigService.load() não foi chamado.")
        return self._merged

    @property
    def identity(self) -> IdentityConfig:
        if self._identity is None:
            raise RuntimeError("ConfigService.load() não foi chamado.")
        return self._identity

    @property
    def pairing(self) -> PairingConfig:
        if self._pairing is None:
            raise RuntimeError("ConfigService.load() não foi chamado.")
        return self._pairing

    def save_user_config(self, user: UserConfig) -> None:
        """Salva user_config.json e recarrega o merge."""
        _save_json(USER_CONFIG_FILE, user.model_dump())
        self._user = user
        self._merged = self._merge()
        logger.info("user_config.json salvo.")

    def save_pending(self, user: UserConfig) -> None:
        """Salva pending_config.json para aplicação após reboot."""
        _save_json(PENDING_CONFIG_FILE, user.model_dump())
        logger.info("pending_config.json salvo.")

    def promote_pending(self) -> bool:
        """Promove pending_config.json para user_config.json se existir."""
        if not PENDING_CONFIG_FILE.exists():
            return False
        try:
            data = _load_json(PENDING_CONFIG_FILE)
            _save_json(USER_CONFIG_FILE, data)
            PENDING_CONFIG_FILE.unlink()
            self._user = UserConfig(**data)
            self._merged = self._merge()
            logger.info("pending_config promovido para user_config.")
            return True
        except Exception as e:
            logger.error("Erro ao promover pending_config: %s", e)
            return False

    def update_last_media(self, filename: str) -> None:
        """Atualiza o último arquivo de mídia tocado."""
        if self._user:
            self._user.last_media = filename
            _save_json(USER_CONFIG_FILE, self._user.model_dump())
            if self._merged:
                self._merged.last_media = filename


config_service = ConfigService()
