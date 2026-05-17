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

MEDIA_DIR        = Path("/opt/av-signage/media/local")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ROTATION_DEGREES = {"normal": 0, "right": 90, "left": 270}
PLAYLIST_FILE    = Path("/tmp/av_signage_playlist.m3u")
CLOCK_SCRIPT     = Path("/opt/av-signage/scripts/clock_overlay.lua")
STANDBY_FILES    = {"status_screen.png", "default_bg_landscape.png", "default_bg_portrait.png", "default_bg.png", "ap_screen.png"}


def _get_rotation() -> int:
    try:
        from app.services.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


def _get_clock_position() -> str:
    try:
        from app.services.config_service import config_service
        return config_service.config.player.clock_position
    except Exception:
        return "top-right"


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


class PlayerService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: str = ""
        self._loop: bool = True
        self._display: Optional["DisplayService"] = None

        self._playlist_items: List[dict] = []
        self._playlist_index: int = 0
        self._playlist_loop: bool = True
        self._playlist_id: str = ""
        self._playlist_name: str = ""
        self._playing_playlist: bool = False
        self._stinger: str = ""
        self._stinger_valid: bool = False

        self._monitor_thread: Optional[threading.Thread] = None
        self._monitoring: bool = False

    def set_screen_service(self, display: "DisplayService") -> None:
        self._display = display

    # -------------------------------------------------------------------------
    # Process management — no IPC, one process per content
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

    def _base_cmd(self) -> list:
        return [
            "mpv",
            "--vo=drm", "--drm-device=/dev/dri/card1",
            "--hwdec=no", "--fs",
            "--no-border", "--no-osc", "--no-input-terminal",
            f"--video-rotate={_get_rotation()}",
            "--image-display-duration=inf",
        ]

    def _clock_args(self) -> list:
        """Returns --script args for clock overlay (only for standby screens)."""
        pos      = _get_clock_position()
        rotation = _get_rotation()
        if pos == "off" or not CLOCK_SCRIPT.exists():
            return []
        return [
            f"--script={CLOCK_SCRIPT}",
            f"--script-opts=clock_overlay-position={pos},clock_overlay-rotation={rotation}",
        ]

    def _launch(self, extra_args: list) -> None:
        """Kill existing mpv and launch with new args."""
        self._kill()
        if self._display:
            self._display.hide()
        time.sleep(0.4)  # Allow DRM device to be fully released before new mpv starts
        self._process = subprocess.Popen(
            self._base_cmd() + extra_args,
            stdout=subprocess.DEVNULL,
            stderr=open("/tmp/mpv_last.log", "w"),  # log for debugging
        )

    # -------------------------------------------------------------------------
    # M3U playlist generation (simple, reliable with mpv)
    # -------------------------------------------------------------------------

    def _generate_playlist(self, items: List[dict], stinger: str = "") -> Path:
        """Generate a plain M3U file. Stinger file is inserted between items if set."""
        stinger_path = MEDIA_DIR / stinger if stinger else None
        stinger_ok   = bool(stinger_path and stinger_path.exists())
        self._stinger_valid = stinger_ok

        lines = []
        for item in items:
            lines.append(str(MEDIA_DIR / item["filename"]))
            if stinger_ok:
                lines.append(str(stinger_path))

        PLAYLIST_FILE.write_text("\n".join(lines), encoding="utf-8")
        logger.debug("Playlist M3U gerada: %d linhas", len(lines))
        return PLAYLIST_FILE

    # -------------------------------------------------------------------------
    # Playback
    # -------------------------------------------------------------------------

    def play(self, filename: str, loop: Optional[bool] = None,
             duration_seconds: int = 10) -> None:
        self._stop_playlist()
        if loop is not None:
            self._loop = loop

        path = MEDIA_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {filename}")

        is_img = _is_image(filename)
        args = [f"--loop-file={'inf' if self._loop else 'no'}"]
        if is_img and not self._loop:
            args.append(f"--image-display-duration={duration_seconds}")
        args.append(str(path))

        self._launch(args)
        self._current_file = filename
        logger.info("Reproduzindo: %s (loop=%s)", filename, self._loop)

        if not self._loop:
            self._start_monitor(single_file=True)

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
        self._playlist_id      = playlist_id
        self._playlist_name    = name
        self._playlist_items   = items
        self._playlist_loop    = loop
        self._playlist_index   = start_index
        self._playing_playlist = True
        self._stinger          = stinger

        pf   = self._generate_playlist(items, stinger)
        step = 2 if self._stinger_valid else 1
        args = [
            f"--loop-playlist={'inf' if loop else 'no'}",
            f"--playlist-start={start_index * step}",
            f"--playlist={str(pf)}",
        ]
        # Stinger de imagem precisa de duração definida; sobrescreve o inf do base_cmd
        if self._stinger_valid and _is_image(stinger):
            args.append(f"--image-display-duration={stinger_duration}")
        self._launch(args)
        self._current_file = items[start_index]["filename"] if items else ""
        self._start_monitor()
        logger.info(
            "Playlist iniciada: %s (%d itens, loop=%s, stinger=%s)",
            playlist_id, len(items), loop, stinger or "none",
        )

    def play_standby(self) -> None:
        """Show standby content: user BG → Maggiore.AV default → status screen."""
        self._playing_playlist = False
        self._current_file     = ""

        standby: Optional[Path] = None

        # 1. User's custom BG media
        try:
            from app.services.config_service import config_service
            bg = config_service.config.bg_media
            if bg:
                p = MEDIA_DIR / bg
                if p.exists():
                    standby = p
        except Exception as e:
            logger.debug("BG media error: %s", e)

        # 2. Status screen with QR code, IP and pairing info
        if not standby and self._display:
            try:
                standby = self._display.render_status_image()
            except Exception as e:
                logger.debug("Status image error: %s", e)

        # 3. Maggiore.AV branded fallback (if status screen fails)
        if not standby and self._display:
            try:
                standby = self._display.generate_default_bg()
            except Exception as e:
                logger.debug("Default BG error: %s", e)

        if standby and standby.exists():
            extra: list = ["--loop-file=inf"]
            # Preenche a tela sem distorção (zoom-crop) para imagens de standby
            if standby.suffix.lower() in IMAGE_EXTENSIONS:
                extra.append("--panscan=1.0")
            extra += self._clock_args()
            extra.append(str(standby))
            self._launch(extra)
            # IMPORTANTE: definir _current_file para is_playing() retornar True
            # e evitar que o scheduler reinicie o mpv a cada 60s
            self._current_file = standby.name
            logger.info("Standby: %s", standby.name)
        else:
            logger.warning("play_standby: sem imagem disponível — mpv não iniciado.")

    def play_image_path(self, path: Path) -> None:
        """Exibe uma imagem a partir de um caminho absoluto (ex.: tela de AP)."""
        self._playing_playlist = False
        self._current_file     = ""
        if not path.exists():
            logger.warning("play_image_path: arquivo não encontrado: %s", path)
            return
        extra: list = ["--loop-file=inf", "--panscan=1.0"]
        extra += self._clock_args()
        extra.append(str(path))
        self._launch(extra)
        self._current_file = path.name
        logger.info("Exibindo imagem: %s", path.name)

    def stop(self, show_status: bool = True) -> None:
        self._stop_playlist()
        self._current_file = ""
        logger.info("Player parado.")
        if show_status:
            self.play_standby()
        else:
            self._kill()

    def restart(self) -> None:
        if not self._current_file and not self._playing_playlist:
            raise RuntimeError("Nenhum arquivo em reprodução.")
        if self._playing_playlist:
            self.play_playlist(
                self._playlist_id, self._playlist_items, self._playlist_loop,
                self._playlist_index, self._stinger, name=self._playlist_name,
            )
        else:
            self.play(self._current_file, loop=self._loop)

    def set_loop(self, enabled: bool) -> None:
        self._loop = enabled
        if self.is_playing() and not self._playing_playlist:
            self.play(self._current_file, loop=enabled)
        logger.info("Loop: %s", "ligado" if enabled else "desligado")

    def next_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        next_idx = (self._playlist_index + 1) % len(self._playlist_items)
        self.play_playlist(
            self._playlist_id, self._playlist_items, self._playlist_loop,
            next_idx, self._stinger, name=self._playlist_name,
        )

    def previous_item(self) -> None:
        if not self._playing_playlist:
            raise RuntimeError("Nenhuma playlist em reprodução.")
        prev_idx = max(0, self._playlist_index - 1)
        self.play_playlist(
            self._playlist_id, self._playlist_items, self._playlist_loop,
            prev_idx, self._stinger, name=self._playlist_name,
        )

    def _stop_playlist(self) -> None:
        self._monitoring      = False
        self._playing_playlist = False
        self._playlist_items  = []
        self._playlist_index  = 0
        self._playlist_id     = ""
        self._playlist_name   = ""
        self._stinger         = ""
        self._stinger_valid   = False

    # -------------------------------------------------------------------------
    # Monitor thread — watches process.poll() only, zero IPC
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
        while self._monitoring:
            time.sleep(1.5)

            proc = self._process
            if proc is None or proc.poll() is not None:
                # mpv exited (natural end or crash)
                if self._playing_playlist and self._playlist_loop:
                    logger.warning("mpv encerrou inesperadamente (playlist loop); recovery.")
                elif self._playing_playlist:
                    logger.info("Playlist finalizada: %s", self._playlist_id)
                elif single_file:
                    logger.info("Arquivo único finalizado; indo para standby.")
                else:
                    break

                self._playing_playlist = False
                self._current_file     = ""
                self.play_standby()
                break

    # -------------------------------------------------------------------------
    # State
    # -------------------------------------------------------------------------

    @property
    def current_playlist_id(self) -> str:
        return self._playlist_id if self._playing_playlist else ""

    def mpv_alive(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def is_playing(self) -> bool:
        """Non-blocking — no IPC calls."""
        return (
            self._process is not None
            and self._process.poll() is None
            and bool(self._current_file or self._playing_playlist)
        )

    def status(self) -> dict:
        playing = self.is_playing()
        standby = (
            playing
            and not self._playing_playlist
            and self._current_file in STANDBY_FILES
        )
        return {
            "playing":          playing,
            "standby":          standby,
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
