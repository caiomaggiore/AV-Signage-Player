from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path("/opt/av-signage")
CONFIG_DIR = BASE_DIR / "config"
MEDIA_LOCAL = BASE_DIR / "media" / "local"
MEDIA_FACTORY = BASE_DIR / "media" / "factory"
MEDIA_BACKUP = BASE_DIR / "media" / "backup"
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


def _backup_user_media() -> dict:
    """Copia todas as mídias de media/local para media/backup antes do factory reset.

    Mantém apenas o backup mais recente para não consumir disco desnecessariamente.
    Retorna dict com contagem e caminho do backup criado.
    """
    import time

    if not MEDIA_LOCAL.exists():
        return {"backed_up": 0, "backup_path": None}

    files = [f for f in MEDIA_LOCAL.iterdir() if f.is_file() and f.stat().st_size > 0]
    if not files:
        return {"backed_up": 0, "backup_path": None}

    timestamp = int(time.time())
    backup_dir = MEDIA_BACKUP / str(timestamp)
    backup_dir.mkdir(parents=True, exist_ok=True)

    for src in files:
        shutil.copy2(src, backup_dir / src.name)

    # Remover backups antigos — manter apenas o último
    all_backups = sorted(
        (d for d in MEDIA_BACKUP.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    for old in all_backups[:-1]:
        shutil.rmtree(old, ignore_errors=True)

    logger.info("Backup de %d mídias criado em %s", len(files), backup_dir)
    return {"backed_up": len(files), "backup_path": str(backup_dir)}


def _restore_factory_media(overwrite: bool = False) -> int:
    """Copia vídeos de fábrica para media/local. Se overwrite=True, substitui existentes."""
    if not MEDIA_FACTORY.exists():
        return 0
    MEDIA_LOCAL.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in MEDIA_FACTORY.iterdir():
        if src.is_file():
            dest = MEDIA_LOCAL / src.name
            if overwrite or not dest.exists():
                shutil.copy2(src, dest)
                count += 1
                logger.info("Mídia de fábrica restaurada: %s", src.name)
    return count


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

    # Restaurar mídias de fábrica que o usuário possa ter removido (sem sobrescrever existentes)
    restored_media = _restore_factory_media(overwrite=False)

    logger.info("Reset de configuração concluído. Removidos: %s. Mídias de fábrica restauradas: %d.", removed, restored_media)
    return {
        "ok": True,
        "type": "reset_config",
        "removed_files": removed,
        "media_preserved": True,
        "factory_media_restored": restored_media,
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

    # Backup antes de apagar — permite recuperação manual em caso de factory reset acidental
    backup_result = _backup_user_media()

    removed_media = 0
    for media_dir in [MEDIA_LOCAL, MEDIA_SERVER, MEDIA_CACHE, MEDIA_DOWNLOADING]:
        removed_media += _clear_dir(media_dir)

    # Restaurar hostname padrão
    try:
        subprocess.run(["sudo", "hostnamectl", "set-hostname", "signage-maggiore"],
                       timeout=5, check=True, capture_output=True)
    except Exception as e:
        logger.warning("Não foi possível restaurar hostname: %s", e)

    # Sempre restaurar mídias de fábrica após factory reset
    restored_media = _restore_factory_media(overwrite=True)

    logger.info(
        "Factory reset concluído. Config removidos: %s. Mídias removidas: %d. "
        "Backup: %d arquivos em %s. Fábrica restaurada: %d.",
        removed_files, removed_media,
        backup_result["backed_up"], backup_result["backup_path"],
        restored_media,
    )
    return {
        "ok": True,
        "type": "factory_reset",
        "removed_files": removed_files,
        "removed_media_count": removed_media,
        "backup": backup_result,
        "factory_media_restored": restored_media,
    }


def reboot() -> None:
    """Executa reboot do sistema."""
    logger.info("Reboot solicitado.")
    subprocess.Popen(["sudo", "reboot"])
