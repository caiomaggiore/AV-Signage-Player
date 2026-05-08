from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

MEDIA_DIR = Path("/opt/av-signage/media/local")
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv"}
MAX_UPLOAD_BYTES = 2048 * 1024 * 1024  # 2 GB


def _ensure_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def list_media() -> List[dict]:
    """Retorna lista de arquivos de mídia com nome e tamanho."""
    _ensure_dir()
    files = []
    for f in sorted(MEDIA_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            size = f.stat().st_size
            files.append({
                "name": f.name,
                "size_mb": round(size / 1_048_576, 1),
                "path": str(f),
            })
    return files


def get_free_bytes() -> int:
    usage = shutil.disk_usage(str(MEDIA_DIR.parent))
    return usage.free


def file_exists(filename: str) -> bool:
    return (MEDIA_DIR / filename).exists()


def validate_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def save_upload(filename: str, data: bytes) -> Path:
    """Salva arquivo de upload em disco. Lança ValueError em caso de erro."""
    _ensure_dir()

    if not validate_extension(filename):
        raise ValueError(f"Extensão não permitida. Use: {', '.join(ALLOWED_EXTENSIONS)}")

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Arquivo excede o tamanho máximo de 2 GB.")

    if len(data) > get_free_bytes():
        free_mb = get_free_bytes() // 1_048_576
        raise ValueError(f"Espaço insuficiente em disco. Livre: {free_mb} MB.")

    dest = MEDIA_DIR / filename
    dest.write_bytes(data)
    logger.info("Mídia salva: %s (%.1f MB)", filename, len(data) / 1_048_576)
    return dest


def delete_media(filename: str) -> None:
    """Remove arquivo de mídia. Lança FileNotFoundError se não existir."""
    path = MEDIA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {filename}")
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Tipo de arquivo não permitido para remoção.")
    path.unlink()
    logger.info("Mídia removida: %s", filename)


media_service_instance = object()  # placeholder — funções são usadas diretamente
