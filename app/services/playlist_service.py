from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

PLAYLISTS_FILE = Path("/opt/av-signage/config/playlists.json")


def _load() -> dict:
    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"playlists": []}
    except json.JSONDecodeError as e:
        logger.error("JSON inválido em playlists.json: %s", e)
        return {"playlists": []}


def _save(data: dict) -> None:
    PLAYLISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYLISTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_playlists() -> List[dict]:
    return _load().get("playlists", [])


def get_playlist(playlist_id: str) -> Optional[dict]:
    for pl in _load().get("playlists", []):
        if pl.get("id") == playlist_id:
            return pl
    return None


def create_playlist(data: dict) -> dict:
    if not data.get("id"):
        data["id"] = f"pl_{uuid.uuid4().hex[:8]}"
    if not data.get("name"):
        raise ValueError("Playlist precisa de um nome.")

    stored = _load()
    # Garantir que ID é único
    existing_ids = {pl["id"] for pl in stored.get("playlists", [])}
    if data["id"] in existing_ids:
        data["id"] = f"pl_{uuid.uuid4().hex[:8]}"

    playlist = _validate_playlist(data)
    stored.setdefault("playlists", []).append(playlist)
    _save(stored)
    logger.info("Playlist criada: %s (%s)", playlist["id"], playlist["name"])
    return playlist


def update_playlist(playlist_id: str, data: dict) -> dict:
    stored = _load()
    playlists = stored.get("playlists", [])
    for i, pl in enumerate(playlists):
        if pl.get("id") == playlist_id:
            data["id"] = playlist_id
            updated = _validate_playlist({**pl, **data})
            playlists[i] = updated
            _save(stored)
            logger.info("Playlist atualizada: %s", playlist_id)
            return updated
    raise KeyError(f"Playlist não encontrada: {playlist_id}")


def delete_playlist(playlist_id: str) -> None:
    stored = _load()
    playlists = stored.get("playlists", [])
    new_list = [pl for pl in playlists if pl.get("id") != playlist_id]
    if len(new_list) == len(playlists):
        raise KeyError(f"Playlist não encontrada: {playlist_id}")
    stored["playlists"] = new_list
    _save(stored)
    logger.info("Playlist removida: %s", playlist_id)


def _validate_playlist(data: dict) -> dict:
    from app.services.media_service import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    items = []
    for item in data.get("items", []):
        filename = str(item.get("filename", "")).strip()
        if not filename:
            continue
        ext = Path(filename).suffix.lower()
        auto_type = "image" if ext in IMAGE_EXTENSIONS else "video"
        item_type = item.get("type", auto_type)
        items.append({
            "type": item_type,
            "filename": filename,
            "duration_seconds": int(item.get("duration_seconds", 10)),
        })

    return {
        "id":               data.get("id", ""),
        "name":             data.get("name", ""),
        "loop":             bool(data.get("loop", True)),
        "stinger":          str(data.get("stinger", "")),
        "stinger_duration": int(data.get("stinger_duration", 1)),
        "items":            items,
    }
