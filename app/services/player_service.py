from __future__ import annotations

import json
import logging
import socket as _sock
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.services.display_service import DisplayService

logger = logging.getLogger(__name__)

MEDIA_DIR   = Path("/opt/av-signage/media/local")
CACHE_DIR   = Path("/opt/av-signage/media/cache")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ROTATION_DEGREES = {"normal": 0, "right": 90, "left": 270}
SOCKET_PATH = "/tmp/mpvsocket"
BLACK_PNG   = CACHE_DIR / "black.png"
FADE_SECS   = 0.45   # duration of black frame for fade transition


def _get_rotation() -> int:
    try:
        from app.services.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _ensure_black_png() -> None:
    """Generate a 1920×1080 black PNG used for fade transitions."""
    if BLACK_PNG.exists():
        return
    try:
        from PIL import Image
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (0, 0, 0)).save(str(BLACK_PNG))
    except Exception as e:
        logger.warning("Não foi possível gerar black.png: %s", e)


class PlayerService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: str = ""
        self._loop: bool = True
        self._display: Optional["DisplayService"] = None

        # Playlist state
        self._playlist_items: List[dict] = []
        self._playlist_index: int = 0
        self._playlist_loop: bool = True
        self._playlist_id: str = ""
        self._playlist_name: str = ""
        self._playing_playlist: bool = False
        self._transition: str = "cut"

        # Monitor thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitoring: bool = False
        self._transitioning: bool = False  # true during fade black frame

        _ensure_black_png()

    def set_screen_service(self, display: "DisplayService") -> None:
        self._display = display

    # -------------------------------------------------------------------------
    # IPC helpers
    # -------------------------------------------------------------------------

    def _ensure_mpv(self) -> None:
        """Start a persistent mpv daemon if not already running."""
        if self._process and self._process.poll() is None:
            return
        Path(SOCKET_PATH).unlink(missing_ok=True)
        rotation = _get_rotation()
        cmd = [
            "mpv",
            "--vo=drm", "--drm-device=/dev/dri/card1",
            "--hwdec=no", "--fs",
            "--no-border", "--no-osc", "--no-input-terminal",
            f"--video-rotate={rotation}",
            "--idle=yes",
            f"--input-ipc-server={SOCKET_PATH}",
        ]
        if self._display:
            self._display.hide()
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Wait until IPC socket is ready
        for _ in range(50):
            if Path(SOCKET_PATH).exists():
                time.sleep(0.05)
                break
            time.sleep(0.1)
        logger.info("mpv daemon iniciado (IPC: %s).", SOCKET_PATH)

    def _ipc(self, *args) -> Optional[dict]:
        """Send a JSON IPC command to the running mpv daemon."""
        try:
            with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(SOCKET_PATH)
                s.send((json.dumps({"command": list(args)}) + "\n").encode())
                raw = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                    if b"\n" in raw:
                        break
                return json.loads(raw.split(b"\n")[0])
        except Exception as e:
            logger.debug("IPC error: %s", e)
            return None

    def _ipc_get(self, prop: str):
        r = self._ipc("get_property", prop)
        if r and r.get("error") == "success":
            return r.get("data")
        return None

    def _kill(self) -> None:
        """Terminate the persistent mpv daemon completely."""
        self._monitoring = False
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("mpv daemon encerrado.")
        self._process = None
        Path(SOCKET_PATH).unlink(missing_ok=True)

    # -------------------------------------------------------------------------
    # Single file playback
    # -------------------------------------------------------------------------

    def play(self, filename: str, loop: Optional[bool] = None, duration_seconds: int = 10) -> None:
        self._stop_playlist()
        self._ensure_mpv()

        if loop is not None:
            self._loop = loop

        path = MEDIA_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {filename}")

        is_img = _is_image(filename)
        loop_val = "inf" if self._loop else "no"
        self._ipc("set_property", "loop-file", loop_val)
        if is_img:
            self._ipc("set_property", "image-display-duration",
                      duration_seconds if not self._loop else "inf")
        self._ipc("loadfile", str(path), "replace")
        self._current_file = filename
        logger.info("Reproduzindo: %s (loop=%s, image=%s)", filename, self._loop, is_img)

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
            self._ipc("set_property", "loop-file", "inf" if enabled else "no")
        logger.info("Loop: %s", "ligado" if enabled else "desligado")

    # -------------------------------------------------------------------------
    # Playlist playback
    # -------------------------------------------------------------------------

    def play_playlist(
        self,
        playlist_id: str,
        items: List[dict],
        loop: bool = True,
        start_index: int = 0,
        transition: str = "cut",
        name: str = "",
    ) -> None:
        if not items:
            logger.warning("Playlist vazia: %s", playlist_id)
            return

        self._stop_playlist()
        self._ensure_mpv()

        self._playlist_id    = playlist_id
        self._playlist_name  = name
        self._playlist_items = items
        self._playlist_loop  = loop
        self._playlist_index = start_index
        self._playing_playlist = True
        self._transition     = transition

        self._play_playlist_item(start_index)
        self._start_monitor()
        logger.info(
            "Playlist iniciada: %s (%d itens, loop=%s, transition=%s)",
            playlist_id, len(items), loop, transition,
        )

    def _play_playlist_item(self, index: int) -> None:
        if index < 0 or index >= len(self._playlist_items):
            return
        item     = self._playlist_items[index]
        filename = item["filename"]
        mtype    = item.get("type", "video")
        is_img   = _is_image(filename) or mtype == "image"
        duration = item.get("duration_seconds", 10) if is_img else None

        path = MEDIA_DIR / filename
        if not path.exists():
            logger.warning("Arquivo não encontrado, pulando: %s", filename)
            self._playlist_index = index
            self._advance_playlist()
            return

        self._transitioning = True
        try:
            # Fade: brief black frame before loading next item
            if self._transition == "fade" and BLACK_PNG.exists():
                self._ipc("set_property", "image-display-duration", FADE_SECS)
                self._ipc("loadfile", str(BLACK_PNG), "replace")
                time.sleep(FADE_SECS + 0.05)

            self._ipc("set_property", "loop-file", "no")
            if is_img:
                self._ipc("set_property", "image-display-duration", duration)
            self._ipc("loadfile", str(path), "replace")
            self._current_file   = filename
            self._playlist_index = index
        finally:
            self._transitioning = False

        logger.info(
            "Playlist item %d/%d: %s",
            index + 1, len(self._playlist_items), filename,
        )

    def _advance_playlist(self) -> None:
        next_idx = self._playlist_index + 1
        if next_idx >= len(self._playlist_items):
            if self._playlist_loop:
                next_idx = 0
            else:
                logger.info("Playlist finalizada: %s", self._playlist_id)
                self._stop_playlist()
                self._kill()
                if self._display:
                    self._display.show_status_from_config()
                return
        self._play_playlist_item(next_idx)

    def _stop_playlist(self) -> None:
        self._monitoring = False
        self._playing_playlist = False
        self._playlist_items   = []
        self._playlist_index   = 0
        self._playlist_id      = ""
        self._playlist_name    = ""

    def next_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        next_idx = (self._playlist_index + 1) % len(self._playlist_items)
        self._play_playlist_item(next_idx)

    def previous_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        prev = max(0, self._playlist_index - 1)
        self._play_playlist_item(prev)

    # -------------------------------------------------------------------------
    # Monitor thread — polls core-idle via IPC
    # -------------------------------------------------------------------------

    def _start_monitor(self) -> None:
        self._monitoring = True
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        waiting_for_play = True
        while self._monitoring:
            time.sleep(0.4)
            if not self._playing_playlist:
                break
            if self._transitioning:
                continue
            if self._process and self._process.poll() is not None:
                break

            idle = self._ipc_get("core-idle")
            if idle is None:
                continue

            if waiting_for_play:
                if idle is False:
                    waiting_for_play = False
            else:
                if idle:
                    waiting_for_play = True
                    if self._playing_playlist and self._monitoring:
                        self._advance_playlist()

    # -------------------------------------------------------------------------
    # State
    # -------------------------------------------------------------------------

    def is_playing(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            return False
        if self._transitioning:
            return True
        idle = self._ipc_get("core-idle")
        if idle is None:
            return bool(self._current_file)
        return not idle

    def status(self) -> dict:
        playing = self.is_playing()
        return {
            "playing": playing,
            "current_file":    self._current_file if playing else "",
            "loop":            self._loop,
            "playing_playlist": self._playing_playlist and playing,
            "playlist_id":     self._playlist_id   if self._playing_playlist else "",
            "playlist_name":   self._playlist_name if self._playing_playlist else "",
            "playlist_index":  self._playlist_index if self._playing_playlist else 0,
            "playlist_total":  len(self._playlist_items) if self._playing_playlist else 0,
            "transition":      self._transition,
        }


player_service = PlayerService()
