from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path("/opt/av-signage")
CONFIG_DIR = BASE_DIR / "config"
MEDIA_LOCAL = BASE_DIR / "media" / "local"
MEDIA_SERVER = BASE_DIR / "media" / "server"
MEDIA_CACHE = BASE_DIR / "media" / "cache"
MEDIA_DOWNLOADING = BASE_DIR / "media" / "downloading"

# Arquivos que nunca devem ser apagados
PROTECTED_FILES = {"defaults.json"}

# Arquivos de configuração do usuário
USER_CONFIG_FILES = [
    CONFIG_DIR / "user_config.json",
    CONFIG_DIR / "pending_config.json",
    CONFIG_DIR / "pairing.json",
    CONFIG_DIR / "auth.json",
]


def _clear_dir(path: Path) -> int:
    """Remove todos os arquivos dentro do diretório. Retorna quantidade removida."""
    count = 0
    if not path.exists():
        return 0
    for item in path.iterdir():
        if item.is_file():
            item.unlink()
            count += 1
        elif item.is_dir():
            shutil.rmtree(item)
            count += 1
    return count


def _remove_file(path: Path) -> bool:
    """Remove arquivo se existir. Retorna True se removido."""
    if path.exists() and path.name not in PROTECTED_FILES:
        path.unlink()
        logger.debug("Removido: %s", path)
        return True
    return False


def reset_config(stop_player_fn=None) -> dict:
    """
    Reset de configuração — mantém mídias locais.

    Remove:
      - user_config.json
      - pending_config.json
      - pairing.json
      - auth.json (senha)

    Mantém:
      - defaults.json
      - media/local/
      - software instalado
    """
    if stop_player_fn:
        try:
            stop_player_fn()
        except Exception:
            pass

    removed = []
    for f in USER_CONFIG_FILES:
        if _remove_file(f):
            removed.append(f.name)

    # Restaurar hostname padrão
    try:
        subprocess.run(["sudo", "hostnamectl", "set-hostname", "signage-maggiore"],
                       timeout=5, check=True, capture_output=True)
        logger.info("Hostname restaurado para signage-maggiore.")
    except Exception as e:
        logger.warning("Não foi possível restaurar hostname: %s", e)

    logger.info("Reset de configuração concluído. Removidos: %s", removed)
    return {
        "ok": True,
        "type": "reset_config",
        "removed_files": removed,
        "media_preserved": True,
    }


def factory_reset(stop_player_fn=None) -> dict:
    """
    Factory reset completo — apaga configurações E mídias.

    Remove:
      - user_config.json
      - pending_config.json
      - pairing.json
      - auth.json
      - media/local/*
      - media/server/*
      - media/cache/*
      - media/downloading/*

    Mantém:
      - defaults.json
      - software instalado
      - serviço systemd
    """
    if stop_player_fn:
        try:
            stop_player_fn()
        except Exception:
            pass

    removed_files = []
    for f in USER_CONFIG_FILES:
        if _remove_file(f):
            removed_files.append(f.name)

    removed_media = 0
    for media_dir in [MEDIA_LOCAL, MEDIA_SERVER, MEDIA_CACHE, MEDIA_DOWNLOADING]:
        removed_media += _clear_dir(media_dir)

    # Restaurar hostname padrão
    try:
        subprocess.run(["sudo", "hostnamectl", "set-hostname", "signage-maggiore"],
                       timeout=5, check=True, capture_output=True)
    except Exception as e:
        logger.warning("Não foi possível restaurar hostname: %s", e)

    logger.info("Factory reset concluído. Arquivos config removidos: %s. Mídias removidas: %d.", removed_files, removed_media)
    return {
        "ok": True,
        "type": "factory_reset",
        "removed_files": removed_files,
        "removed_media_count": removed_media,
    }


def reboot() -> None:
    """Executa reboot do sistema."""
    logger.info("Reboot solicitado.")
    subprocess.Popen(["sudo", "reboot"])
