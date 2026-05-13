from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

AUTH_FILE = Path("/opt/av-signage/config/auth.json")

SESSION_LIFETIME = 3600
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 900


def _load_auth() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_auth(data: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


class AuthService:
    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._attempts: dict[str, list[float]] = {}

    def is_first_access(self) -> bool:
        data = _load_auth()
        return not data.get("password_hash")

    def set_password(self, password: str) -> None:
        if len(password) < 6:
            raise ValueError("A senha deve ter no mínimo 6 caracteres.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        data = _load_auth()
        data["password_hash"] = hashed
        _save_auth(data)
        logger.info("Senha configurada.")

    def verify_password(self, password: str) -> bool:
        data = _load_auth()
        stored = data.get("password_hash", "")
        if not stored:
            return False
        return bcrypt.checkpw(password.encode(), stored.encode())

    def is_locked_out(self, client_ip: str) -> bool:
        now = time.time()
        attempts = [t for t in self._attempts.get(client_ip, []) if now - t < LOCKOUT_SECONDS]
        self._attempts[client_ip] = attempts
        return len(attempts) >= MAX_ATTEMPTS

    def record_failed_attempt(self, client_ip: str) -> int:
        now = time.time()
        attempts = [t for t in self._attempts.get(client_ip, []) if now - t < LOCKOUT_SECONDS]
        attempts.append(now)
        self._attempts[client_ip] = attempts
        remaining = max(0, MAX_ATTEMPTS - len(attempts))
        logger.warning("Tentativa falha de login do IP %s. Restam %d tentativas.", client_ip, remaining)
        return remaining

    def clear_attempts(self, client_ip: str) -> None:
        self._attempts.pop(client_ip, None)

    def create_session(self) -> str:
        token = secrets.token_hex(32)
        self._sessions[token] = time.time()
        return token

    def validate_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        created_at = self._sessions.get(token)
        if created_at is None:
            return False
        if time.time() - created_at > SESSION_LIFETIME:
            self._sessions.pop(token, None)
            return False
        self._sessions[token] = time.time()
        return True

    def invalidate_session(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)

    def reset_password(self) -> None:
        data = _load_auth()
        data.pop("password_hash", None)
        _save_auth(data)
        self._sessions.clear()
        logger.info("Senha removida — primeiro acesso será exigido novamente.")


auth_service = AuthService()
