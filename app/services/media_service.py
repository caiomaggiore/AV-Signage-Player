from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

BASE_DIR = Path("/opt/av-signage")
MEDIA_LOCAL = BASE_DIR / "media" / "local"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

MAX_UPLOAD_MB = 2048
SAFE_FILENAME_RE = re.compile(r"^[\w\s\-.()\[\]]+$", re.UNICODE)


def _safe_filename(name: str) -> bool:
    return bool(SAFE_FILENAME_RE.match(name)) and ".." not in name and "/" not in name


def detect_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "video"


def list_media() -> List[dict]:
    MEDIA_LOCAL.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(MEDIA_LOCAL.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            stat = f.stat()
            files.append({
                "name": f.name,
                "type": detect_type(f.name),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / 1_048_576, 1),
                "modified_ts": stat.st_mtime,
                "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return files


def file_exists(filename: str) -> bool:
    return (MEDIA_LOCAL / filename).exists()


def save_upload(filename: str, data: bytes) -> Path:
    if not _safe_filename(filename):
        raise ValueError(f"Nome de arquivo inválido: {filename!r}")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Extensão não suportada: {ext!r}. "
            f"Permitidas: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    if len(data) == 0:
        raise ValueError("Arquivo vazio. O upload não contém dados.")

    if len(data) > MAX_UPLOAD_MB * 1_048_576:
        raise ValueError(f"Arquivo muito grande. Limite: {MAX_UPLOAD_MB} MB.")

    MEDIA_LOCAL.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_LOCAL / filename
    dest.write_bytes(data)
    logger.info("Arquivo salvo: %s (%d bytes)", filename, len(data))
    return dest


def delete_media(filename: str) -> None:
    if not _safe_filename(filename):
        raise ValueError(f"Nome de arquivo inválido: {filename!r}")

    path = MEDIA_LOCAL / filename
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {filename!r}")

    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Tipo de arquivo não gerenciado: {ext!r}")

    path.unlink()
    logger.info("Arquivo removido: %s", filename)


def get_free_bytes() -> int:
    try:
        stat = os.statvfs(str(MEDIA_LOCAL))
        return stat.f_bavail * stat.f_frsize
    except Exception:
        return 0


def get_file_path(filename: str) -> Path:
    if not _safe_filename(filename):
        raise ValueError(f"Nome de arquivo inválido: {filename!r}")
    path = MEDIA_LOCAL / filename
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {filename!r}")
    return path
