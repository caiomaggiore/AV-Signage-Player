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

PROTECTED_FILES = {"defaults.json"}

USER_CONFIG_FILES = [
    CONFIG_DIR / "user_config.json",
    CONFIG_DIR / "pending_config.json",
    CONFIG_DIR / "pairing.json",
    CONFIG_DIR / "auth.json",
    CONFIG_DIR / "state.json",
]


def _backup_user_media() -> dict:
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

    all_backups = sorted(
        (d for d in MEDIA_BACKUP.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    for old in all_backups[:-1]:
        shutil.rmtree(old, ignore_errors=True)

    logger.info("Backup de %d mídias criado em %s", len(files), backup_dir)
    return {"backed_up": len(files), "backup_path": str(backup_dir)}


def _restore_factory_media(overwrite: bool = False) -> int:
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
    return count


def _clear_dir(path: Path) -> int:
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
    if path.exists() and path.name not in PROTECTED_FILES:
        path.unlink()
        return True
    return False


def reset_config(stop_player_fn=None) -> dict:
    if stop_player_fn:
        try:
            stop_player_fn()
        except Exception:
            pass

    removed = []
    for f in USER_CONFIG_FILES:
        if _remove_file(f):
            removed.append(f.name)

    # Também remover playlists e schedules do usuário
    for extra in [CONFIG_DIR / "playlists.json", CONFIG_DIR / "schedules.json"]:
        if _remove_file(extra):
            removed.append(extra.name)

    try:
        subprocess.run(["sudo", "hostnamectl", "set-hostname", "signage-maggiore"],
                       timeout=5, check=True, capture_output=True)
    except Exception as e:
        logger.warning("Não foi possível restaurar hostname: %s", e)

    restored_media = _restore_factory_media(overwrite=False)

    logger.info("Reset de configuração concluído. Removidos: %s.", removed)
    return {
        "ok": True,
        "type": "reset_config",
        "removed_files": removed,
        "media_preserved": True,
        "factory_media_restored": restored_media,
    }


def factory_reset(stop_player_fn=None) -> dict:
    if stop_player_fn:
        try:
            stop_player_fn()
        except Exception:
            pass

    removed_files = []
    for f in USER_CONFIG_FILES:
        if _remove_file(f):
            removed_files.append(f.name)

    for extra in [CONFIG_DIR / "playlists.json", CONFIG_DIR / "schedules.json"]:
        if _remove_file(extra):
            removed_files.append(extra.name)

    backup_result = _backup_user_media()

    removed_media = 0
    for media_dir in [MEDIA_LOCAL, MEDIA_SERVER, MEDIA_CACHE, MEDIA_DOWNLOADING]:
        removed_media += _clear_dir(media_dir)

    try:
        subprocess.run(["sudo", "hostnamectl", "set-hostname", "signage-maggiore"],
                       timeout=5, check=True, capture_output=True)
    except Exception as e:
        logger.warning("Não foi possível restaurar hostname: %s", e)

    restored_media = _restore_factory_media(overwrite=True)

    logger.info(
        "Factory reset concluído. Config: %s. Mídias: %d. Backup: %d arquivos.",
        removed_files, removed_media, backup_result["backed_up"],
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
    logger.info("Reboot solicitado.")
    subprocess.Popen(["sudo", "reboot"])
