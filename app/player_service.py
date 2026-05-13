from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.screen_service import ScreenService

logger = logging.getLogger(__name__)

MEDIA_DIR = Path("/opt/av-signage/media/local")

ROTATION_DEGREES = {"normal": 0, "right": 90, "left": 270}


def _get_rotation() -> int:
    """Retorna os graus de rotação configurados. 0 se não configurado."""
    try:
        from app.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


class PlayerService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: str = ""
        self._loop: bool = True
        self._screen: Optional["ScreenService"] = None

    def set_screen_service(self, screen: "ScreenService") -> None:
        """Registra o ScreenService para controle automático."""
        self._screen = screen

    def _kill(self) -> None:
        """Encerra o processo mpv se estiver rodando."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("mpv encerrado.")
        self._process = None

    def play(self, filename: str, loop: Optional[bool] = None) -> None:
        """Inicia reprodução de arquivo em fullscreen via mpv."""
        path = MEDIA_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {filename}")

        self._kill()

        if loop is not None:
            self._loop = loop

        rotation = _get_rotation()
        cmd = [
            "mpv",
            "--fs",
            "--no-border",
            "--no-osc",
            "--no-input-terminal",
            "--vo=drm",
            "--drm-device=/dev/dri/card1",
            "--hwdec=no",
            f"--loop-file={'inf' if self._loop else 'no'}",
            f"--video-rotate={rotation}",
            str(path),
        ]

        if self._screen:
            self._screen.hide()

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._current_file = filename
        logger.info("Reproduzindo: %s (loop=%s, rotation=%s°)", filename, self._loop, rotation)

    def stop(self, show_status: bool = True) -> None:
        """Para a reprodução e opcionalmente exibe tela de status."""
        self._kill()
        self._current_file = ""
        logger.info("Player parado.")
        if show_status and self._screen:
            self._screen.show_status_from_config()

    def restart(self) -> None:
        """Reinicia o vídeo atual do início."""
        if not self._current_file:
            raise RuntimeError("Nenhum arquivo em reprodução.")
        self.play(self._current_file, loop=self._loop)
        logger.info("Vídeo reiniciado: %s", self._current_file)

    def set_loop(self, enabled: bool) -> None:
        """Altera o modo loop. Reinicia o player se estiver rodando."""
        self._loop = enabled
        if self.is_playing():
            self.play(self._current_file, loop=enabled)
        logger.info("Loop: %s", "ligado" if enabled else "desligado")

    def is_playing(self) -> bool:
        """Retorna True se o mpv está rodando."""
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict:
        return {
            "playing": self.is_playing(),
            "current_file": self._current_file if self.is_playing() else "",
            "loop": self._loop,
        }


player_service = PlayerService()
