from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

SCHEDULES_FILE = Path("/opt/av-signage/config/schedules.json")

WEEKDAY_MAP = {
    0: "mon", 1: "tue", 2: "wed", 3: "thu",
    4: "fri", 5: "sat", 6: "sun",
}


def _load() -> dict:
    try:
        return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schedules": [], "fallback_playlist_id": ""}
    except json.JSONDecodeError as e:
        logger.error("JSON inválido em schedules.json: %s", e)
        return {"schedules": [], "fallback_playlist_id": ""}


def _save(data: dict) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_schedules() -> List[dict]:
    return _load().get("schedules", [])


def get_schedule(schedule_id: str) -> Optional[dict]:
    for sc in _load().get("schedules", []):
        if sc.get("id") == schedule_id:
            return sc
    return None


def get_fallback_playlist_id() -> str:
    return _load().get("fallback_playlist_id", "")


def set_fallback_playlist_id(playlist_id: str) -> None:
    stored = _load()
    stored["fallback_playlist_id"] = playlist_id
    _save(stored)


def create_schedule(data: dict) -> dict:
    if not data.get("id"):
        data["id"] = f"sch_{uuid.uuid4().hex[:8]}"
    stored = _load()
    schedule = _validate_schedule(data)
    stored.setdefault("schedules", []).append(schedule)
    _save(stored)
    logger.info("Agendamento criado: %s (%s)", schedule["id"], schedule["name"])
    return schedule


def update_schedule(schedule_id: str, data: dict) -> dict:
    stored = _load()
    schedules = stored.get("schedules", [])
    for i, sc in enumerate(schedules):
        if sc.get("id") == schedule_id:
            data["id"] = schedule_id
            updated = _validate_schedule({**sc, **data})
            schedules[i] = updated
            _save(stored)
            return updated
    raise KeyError(f"Agendamento não encontrado: {schedule_id}")


def delete_schedule(schedule_id: str) -> None:
    stored = _load()
    schedules = stored.get("schedules", [])
    new_list = [sc for sc in schedules if sc.get("id") != schedule_id]
    if len(new_list) == len(schedules):
        raise KeyError(f"Agendamento não encontrado: {schedule_id}")
    stored["schedules"] = new_list
    _save(stored)
    logger.info("Agendamento removido: %s", schedule_id)


def get_active_schedule(now: Optional[datetime] = None) -> Optional[dict]:
    """Retorna o agendamento ativo no momento atual, ou None."""
    if now is None:
        now = datetime.now()

    weekday_key = WEEKDAY_MAP.get(now.weekday(), "")
    current_time = now.strftime("%H:%M")

    for sc in _load().get("schedules", []):
        if not sc.get("enabled", True):
            continue
        if weekday_key not in sc.get("days", []):
            continue
        start = sc.get("start_time", "00:00")
        end = sc.get("end_time", "23:59")
        if start <= current_time <= end:
            return sc

    return None


def _validate_schedule(data: dict) -> dict:
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    days = [d for d in data.get("days", []) if d in valid_days]

    start = data.get("start_time", "08:00")
    end = data.get("end_time", "18:00")

    # Validar formato HH:MM
    for t in (start, end):
        parts = t.split(":")
        if len(parts) != 2:
            raise ValueError(f"Horário inválido: {t!r}. Use HH:MM.")

    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "playlist_id": data.get("playlist_id", ""),
        "days": days,
        "start_time": start,
        "end_time": end,
        "enabled": bool(data.get("enabled", True)),
    }
