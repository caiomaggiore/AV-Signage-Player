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
EDL_PATH    = Path("/tmp/av_signage_playlist.edl")


def _get_rotation() -> int:
    try:
        from app.services.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _edl_entry(path: Path, duration: Optional[float] = None) -> str:
    """Return one EDL v0 line using %N% encoding for any filename."""
    p = str(path)
    encoded_len = len(p.encode("utf-8"))
    entry = f"%{encoded_len}%{p}"
    if duration is not None:
        entry += f",,{duration}"
    return entry


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
        self._stinger: str = ""
        self._stinger_valid: bool = False

        # Monitor
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitoring: bool = False

    def set_screen_service(self, display: "DisplayService") -> None:
        self._display = display

    # -------------------------------------------------------------------------
    # Persistent mpv daemon
    # -------------------------------------------------------------------------

    def _ensure_mpv(self) -> bool:
        """Start persistent mpv daemon if not running. Returns True if ready."""
        if self._process and self._process.poll() is None:
            return True

        Path(SOCKET_PATH).unlink(missing_ok=True)
        rotation = _get_rotation()
        cmd = [
            "mpv",
            "--vo=drm", "--drm-device=/dev/dri/card1",
            "--hwdec=no", "--fs",
            "--no-border", "--no-osc", "--no-input-terminal",
            f"--video-rotate={rotation}",
            "--idle=yes",
            "--image-display-duration=inf",
            f"--input-ipc-server={SOCKET_PATH}",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            logger.error("mpv não encontrado no PATH.")
            return False

        # Wait up to 8s for socket (DRM device may take a moment on boot)
        for _ in range(80):
            if Path(SOCKET_PATH).exists():
                time.sleep(0.05)
                logger.info("mpv daemon iniciado (pid=%d).", self._process.pid)
                return True
            time.sleep(0.1)

        # If mpv exited already, capture the failure
        if self._process.poll() is not None:
            logger.error("mpv daemon falhou ao iniciar (exit=%d).", self._process.returncode)
            self._process = None
            return False

        logger.warning("mpv daemon iniciou mas socket demorou (continuando mesmo assim).")
        return True

    def _ipc(self, *args) -> Optional[dict]:
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
        """Terminate mpv daemon completely (used for shutdown only)."""
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
    # EDL generation
    # -------------------------------------------------------------------------

    def _generate_edl(self, items: List[dict], stinger: str = "",
                      stinger_duration: int = 1) -> Path:
        """Write an mpv EDL v0 file for the given playlist items."""
        stinger_path = MEDIA_DIR / stinger if stinger else None
        stinger_ok   = bool(stinger_path and stinger_path.exists())

        lines = ["# mpv EDL v0"]
        for item in items:
            path    = MEDIA_DIR / item["filename"]
            is_img  = _is_image(item["filename"]) or item.get("type") == "image"
            dur     = item.get("duration_seconds", 10) if is_img else None
            lines.append(_edl_entry(path, dur))

            if stinger_ok:
                # Images used as stinger need a duration
                s_dur = stinger_duration if _is_image(stinger) else None
                lines.append(_edl_entry(stinger_path, s_dur))

        EDL_PATH.write_text("\n".join(lines), encoding="utf-8")
        return EDL_PATH

    # -------------------------------------------------------------------------
    # Single-file playback
    # -------------------------------------------------------------------------

    def play(self, filename: str, loop: Optional[bool] = None,
             duration_seconds: int = 10) -> None:
        self._stop_playlist()
        if not self._ensure_mpv():
            raise RuntimeError("mpv daemon não pôde ser iniciado.")

        if loop is not None:
            self._loop = loop

        path = MEDIA_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {filename}")

        is_img   = _is_image(filename)
        loop_val = "inf" if self._loop else "no"
        self._ipc("set_property", "loop-playlist", "no")
        self._ipc("set_property", "loop-file", loop_val)

        if is_img and not self._loop:
            # Use mini-EDL so mpv respects the duration
            mini_edl = f"# mpv EDL v0\n{_edl_entry(path, duration_seconds)}"
            EDL_PATH.write_text(mini_edl, encoding="utf-8")
            self._ipc("set_property", "loop-file", "no")
            self._ipc("loadlist", str(EDL_PATH), "replace")
        else:
            self._ipc("loadfile", str(path), "replace")

        self._current_file = filename
        logger.info("Reproduzindo: %s (loop=%s)", filename, self._loop)

        # For non-looping single files, start a monitor to call play_standby when done
        if not self._loop:
            self._playing_playlist = False
            self._start_monitor(single_file=True)

    def stop(self, show_status: bool = True) -> None:
        self._stop_playlist()
        self._current_file = ""
        logger.info("Player parado.")
        if show_status:
            self.play_standby()
        else:
            # On shutdown: terminate mpv cleanly (graceful SIGTERM, not SIGKILL)
            if self.mpv_alive():
                self._kill()

    def restart(self) -> None:
        if not self._current_file:
            raise RuntimeError("Nenhum arquivo em reprodução.")
        if self._playing_playlist:
            self._reload_playlist()
        else:
            self.play(self._current_file, loop=self._loop)

    def set_loop(self, enabled: bool) -> None:
        self._loop = enabled
        if self.is_playing() and not self._playing_playlist:
            self._ipc("set_property", "loop-file", "inf" if enabled else "no")
        logger.info("Loop: %s", "ligado" if enabled else "desligado")

    # -------------------------------------------------------------------------
    # Playlist playback (EDL-based)
    # -------------------------------------------------------------------------

    def play_playlist(
        self,
        playlist_id: str,
        items: List[dict],
        loop: bool = True,
        start_index: int = 0,
        stinger: str = "",
        stinger_duration: int = 1,
        name: str = "",
    ) -> None:
        if not items:
            logger.warning("Playlist vazia: %s", playlist_id)
            return

        self._stop_playlist()
        if not self._ensure_mpv():
            raise RuntimeError("mpv daemon não pôde ser iniciado.")

        self._playlist_id    = playlist_id
        self._playlist_name  = name
        self._playlist_items = items
        self._playlist_loop  = loop
        self._playlist_index = start_index
        self._playing_playlist = True
        self._stinger        = stinger
        self._stinger_valid  = bool(stinger and (MEDIA_DIR / stinger).exists())

        edl = self._generate_edl(items, stinger, stinger_duration)
        self._ipc("set_property", "loop-playlist", "inf" if loop else "no")
        self._ipc("loadlist", str(edl), "replace")

        if start_index > 0:
            step = 2 if self._stinger_valid else 1
            self._ipc("set_property", "playlist-pos", start_index * step)

        self._current_file = items[start_index]["filename"] if items else ""
        self._start_monitor()
        logger.info(
            "Playlist iniciada: %s (%d itens, loop=%s, stinger=%s)",
            playlist_id, len(items), loop, stinger or "none",
        )

    def _reload_playlist(self) -> None:
        """Regenerate and reload EDL keeping current index."""
        edl = self._generate_edl(self._playlist_items, self._stinger)
        self._ipc("set_property", "loop-playlist", "inf" if self._playlist_loop else "no")
        self._ipc("loadlist", str(edl), "replace")
        step = 2 if self._stinger_valid else 1
        self._ipc("set_property", "playlist-pos", self._playlist_index * step)

    def _stop_playlist(self) -> None:
        self._monitoring = False
        self._playing_playlist = False
        self._playlist_items   = []
        self._playlist_index   = 0
        self._playlist_id      = ""
        self._playlist_name    = ""
        self._stinger          = ""
        self._stinger_valid    = False

    def next_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        step  = 2 if self._stinger_valid else 1
        total = len(self._playlist_items)
        next_idx = (self._playlist_index + 1) % total
        self._ipc("set_property", "playlist-pos", next_idx * step)

    def previous_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        step     = 2 if self._stinger_valid else 1
        prev_idx = max(0, self._playlist_index - 1)
        self._ipc("set_property", "playlist-pos", prev_idx * step)

    # -------------------------------------------------------------------------
    # Standby — plays BG media or status screen; never shows Linux console
    # -------------------------------------------------------------------------

    def play_standby(self) -> None:
        """Load standby content into persistent mpv. NEVER kills mpv."""
        self._playing_playlist = False
        self._current_file     = ""

        # 1. Try user's custom BG media
        try:
            from app.services.config_service import config_service
            bg = config_service.config.bg_media
            if bg:
                bg_path = MEDIA_DIR / bg
                if bg_path.exists() and self._ensure_mpv():
                    self._ipc("set_property", "loop-playlist", "no")
                    # Pass loop-file as per-file option in loadfile (more reliable)
                    self._ipc("loadfile", str(bg_path), "replace", 0, "loop-file=inf")
                    logger.info("Standby: BG media → %s", bg)
                    return
        except Exception as e:
            logger.debug("BG media error: %s", e)

        # 2. Render status screen and load it into mpv
        if self._display:
            try:
                status_path = self._display.render_status_image()
                if status_path and status_path.exists() and self._ensure_mpv():
                    self._ipc("set_property", "loop-playlist", "no")
                    self._ipc("loadfile", str(status_path), "replace", 0,
                               "loop-file=inf,image-display-duration=inf")
                    logger.info("Standby: status screen")
                    return
            except Exception as e:
                logger.debug("Status image error: %s", e)

        # 3. Keep mpv alive in idle — black screen is better than the Linux console.
        # NEVER kill mpv here: forceful DRM release can lock up the Pi GPU driver.
        if self.mpv_alive():
            logger.warning("play_standby: sem conteúdo; mantendo mpv em idle (tela preta).")
        else:
            # mpv is dead; restart it in idle so it holds the display
            if self._ensure_mpv():
                logger.warning("play_standby: mpv reiniciado em idle (sem conteúdo).")
            else:
                logger.error("play_standby: mpv não pôde ser iniciado.")

    # -------------------------------------------------------------------------
    # Monitor thread — tracks playlist-pos and end-of-playlist
    # -------------------------------------------------------------------------

    def _start_monitor(self, single_file: bool = False) -> None:
        self._monitoring = True
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(single_file,),
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self, single_file: bool = False) -> None:
        step     = 2 if self._stinger_valid else 1
        was_idle = True  # mpv starts idle; goes non-idle when content loads

        while self._monitoring:
            time.sleep(0.5)

            # mpv crashed — restart in idle to avoid console
            if self._process and self._process.poll() is not None:
                logger.warning("mpv daemon terminou inesperadamente; reiniciando em idle.")
                self._process = None
                self._playing_playlist = False
                self._current_file     = ""
                self.play_standby()
                break

            # Playlist mode: track position
            if self._playing_playlist:
                pos = self._ipc_get("playlist-pos")
                if pos is not None and pos % step == 0:
                    idx = pos // step
                    if 0 <= idx < len(self._playlist_items):
                        if idx != self._playlist_index:
                            self._playlist_index = idx
                            self._current_file   = self._playlist_items[idx]["filename"]

                # End detection for non-looping playlists
                if not self._playlist_loop:
                    idle = self._ipc_get("core-idle")
                    if idle is None:
                        continue
                    if was_idle and not idle:
                        was_idle = False
                    elif not was_idle and idle:
                        logger.info("Playlist não-loop finalizada: %s", self._playlist_id)
                        self._playing_playlist = False
                        self._current_file     = ""
                        self.play_standby()
                        break

            # Single-file mode: detect when non-looping file ends
            elif single_file:
                idle = self._ipc_get("core-idle")
                if idle is None:
                    continue
                if was_idle and not idle:
                    was_idle = False
                elif not was_idle and idle:
                    logger.info("Arquivo único finalizado; indo para standby.")
                    self._current_file = ""
                    self.play_standby()
                    break

            else:
                break  # nothing to monitor

    # -------------------------------------------------------------------------
    # State
    # -------------------------------------------------------------------------

    @property
    def current_playlist_id(self) -> str:
        """Retorna o ID da playlist configurada, sem depender de IPC."""
        return self._playlist_id if self._playing_playlist else ""

    def mpv_alive(self) -> bool:
        """Verifica se o processo mpv está rodando (sem IPC)."""
        return bool(self._process and self._process.poll() is None)

    def is_playing(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            return False
        idle = self._ipc_get("core-idle")
        if idle is None:
            return bool(self._current_file)
        return not idle

    def status(self) -> dict:
        playing = self.is_playing()
        return {
            "playing":          playing,
            "current_file":     self._current_file if playing else "",
            "loop":             self._loop,
            "playing_playlist": self._playing_playlist and playing,
            "playlist_id":      self._playlist_id   if self._playing_playlist else "",
            "playlist_name":    self._playlist_name if self._playing_playlist else "",
            "playlist_index":   self._playlist_index if self._playing_playlist else 0,
            "playlist_total":   len(self._playlist_items) if self._playing_playlist else 0,
            "stinger":          self._stinger,
        }


player_service = PlayerService()
