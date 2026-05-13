from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.services.display_service import DisplayService

logger = logging.getLogger(__name__)

MEDIA_DIR = Path("/opt/av-signage/media/local")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ROTATION_DEGREES = {"normal": 0, "right": 90, "left": 270}


def _get_rotation() -> int:
    try:
        from app.services.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


class PlayerService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: str = ""
        self._loop: bool = True
        self._display: Optional["DisplayService"] = None

        # Estado da playlist
        self._playlist_items: List[dict] = []
        self._playlist_index: int = 0
        self._playlist_loop: bool = True
        self._playlist_id: str = ""
        self._playing_playlist: bool = False

        # Thread de monitoramento para auto-avanço
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitoring: bool = False

    def set_screen_service(self, display: "DisplayService") -> None:
        self._display = display

    # -------------------------------------------------------------------------
    # Controle de processo
    # -------------------------------------------------------------------------

    def _kill(self) -> None:
        self._monitoring = False
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("mpv encerrado.")
        self._process = None

    def _build_cmd(self, path: Path, loop_file: bool, duration_seconds: Optional[int] = None) -> list:
        rotation = _get_rotation()
        cmd = [
            "mpv", "--fs", "--no-border", "--no-osc", "--no-input-terminal",
            "--vo=drm", "--drm-device=/dev/dri/card1", "--hwdec=no",
            f"--loop-file={'inf' if loop_file else 'no'}",
            f"--video-rotate={rotation}",
        ]
        if duration_seconds is not None:
            cmd.append(f"--image-display-duration={duration_seconds}")
        cmd.append(str(path))
        return cmd

    def _launch(self, filename: str, loop_file: bool, duration_seconds: Optional[int] = None) -> None:
        path = MEDIA_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {filename}")
        cmd = self._build_cmd(path, loop_file, duration_seconds)
        if self._display:
            self._display.hide()
        self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._current_file = filename

    # -------------------------------------------------------------------------
    # Reprodução de arquivo único
    # -------------------------------------------------------------------------

    def play(self, filename: str, loop: Optional[bool] = None, duration_seconds: int = 10) -> None:
        self._stop_playlist()
        self._kill()

        if loop is not None:
            self._loop = loop

        is_img = _is_image(filename)
        dur = duration_seconds if is_img else None
        loop_file = False if is_img else self._loop

        self._launch(filename, loop_file, duration_seconds=dur)
        logger.info("Reproduzindo: %s (loop=%s, image=%s)", filename, loop_file, is_img)

    def stop(self, show_status: bool = True) -> None:
        self._stop_playlist()
        self._kill()
        self._current_file = ""
        logger.info("Player parado.")
        if show_status and self._display:
            self._display.show_status_from_config()

    def restart(self) -> None:
        if not self._current_file:
            raise RuntimeError("Nenhum arquivo em reprodução.")
        if self._playing_playlist:
            self._play_playlist_item(self._playlist_index)
        else:
            self.play(self._current_file, loop=self._loop)

    def set_loop(self, enabled: bool) -> None:
        self._loop = enabled
        if self.is_playing() and not self._playing_playlist:
            self.play(self._current_file, loop=enabled)
        logger.info("Loop: %s", "ligado" if enabled else "desligado")

    # -------------------------------------------------------------------------
    # Playlist
    # -------------------------------------------------------------------------

    def play_playlist(self, playlist_id: str, items: List[dict], loop: bool = True, start_index: int = 0) -> None:
        """Inicia reprodução de playlist com auto-avanço."""
        if not items:
            logger.warning("Playlist vazia: %s", playlist_id)
            return

        self._stop_playlist()
        self._kill()

        self._playlist_id = playlist_id
        self._playlist_items = items
        self._playlist_loop = loop
        self._playlist_index = start_index
        self._playing_playlist = True

        self._play_playlist_item(self._playlist_index)
        self._start_monitor()
        logger.info("Playlist iniciada: %s (%d itens, loop=%s)", playlist_id, len(items), loop)

    def _play_playlist_item(self, index: int) -> None:
        if index < 0 or index >= len(self._playlist_items):
            return
        item = self._playlist_items[index]
        filename = item["filename"]
        media_type = item.get("type", "video")
        duration = item.get("duration_seconds", 10) if media_type == "image" else None
        is_img = _is_image(filename) or media_type == "image"
        self._launch(filename, loop_file=False, duration_seconds=duration if is_img else None)
        self._playlist_index = index
        logger.info("Playlist item %d/%d: %s", index + 1, len(self._playlist_items), filename)

    def _advance_playlist(self) -> None:
        next_index = self._playlist_index + 1
        if next_index >= len(self._playlist_items):
            if self._playlist_loop:
                next_index = 0
            else:
                logger.info("Playlist finalizada: %s", self._playlist_id)
                self._stop_playlist()
                if self._display:
                    self._display.show_status_from_config()
                return
        self._play_playlist_item(next_index)

    def _stop_playlist(self) -> None:
        self._monitoring = False
        self._playing_playlist = False
        self._playlist_items = []
        self._playlist_index = 0
        self._playlist_id = ""

    def next_item(self) -> None:
        """Avança para o próximo item da playlist."""
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        self._kill()
        self._advance_playlist()

    def previous_item(self) -> None:
        """Volta para o item anterior da playlist."""
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        self._kill()
        prev = max(0, self._playlist_index - 1)
        self._play_playlist_item(prev)

    # -------------------------------------------------------------------------
    # Thread de monitoramento para auto-avanço
    # -------------------------------------------------------------------------

    def _start_monitor(self) -> None:
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while self._monitoring:
            time.sleep(0.5)
            if not self._playing_playlist:
                break
            proc = self._process
            if proc is not None and proc.poll() is not None:
                # Item terminou — avançar para próximo
                if self._playing_playlist and self._monitoring:
                    self._advance_playlist()

    # -------------------------------------------------------------------------
    # Estado
    # -------------------------------------------------------------------------

    def is_playing(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict:
        playing = self.is_playing()
        return {
            "playing": playing,
            "current_file": self._current_file if playing else "",
            "loop": self._loop,
            "playing_playlist": self._playing_playlist and playing,
            "playlist_id": self._playlist_id if self._playing_playlist else "",
            "playlist_index": self._playlist_index if self._playing_playlist else 0,
            "playlist_total": len(self._playlist_items) if self._playing_playlist else 0,
        }


player_service = PlayerService()
