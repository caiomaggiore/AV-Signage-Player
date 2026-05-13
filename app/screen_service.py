from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_IMAGE = Path("/opt/av-signage/media/cache/status_screen.png")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _generate_image(
    display_name: str,
    hostname: str,
    ip: str,
    pairing_code: str,
    version: str,
) -> bool:
    """Gera PNG de status em paisagem (1920×1080). Retorna True se gerou com sucesso."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        W, H = 1920, 1080
        BG = (10, 12, 20)
        ACCENT = (79, 124, 255)
        TEXT = (232, 234, 240)
        MUTED = (107, 114, 128)
        WHITE = (255, 255, 255)
        SEP = (45, 50, 80)

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                return ImageFont.load_default()

        font_title = load_font(FONT_BOLD, 56)
        font_label = load_font(FONT_PATH, 26)
        font_value = load_font(FONT_BOLD, 32)
        font_small = load_font(FONT_PATH, 22)
        font_code  = load_font(FONT_BOLD, 72)
        font_url   = load_font(FONT_PATH, 30)

        cx = W // 2

        draw.text((cx, 160), "AV Signage Player", font=font_title, fill=ACCENT, anchor="mm")
        draw.text((cx, 240), display_name,        font=font_label, fill=MUTED,  anchor="mm")
        draw.line([(cx - 300, 290), (cx + 300, 290)], fill=SEP, width=1)

        col1_x, col2_x = W // 4, W * 3 // 4
        row1_y, row2_y = 370, 480

        draw.text((col1_x, row1_y - 28), "ENDEREÇO IP",       font=font_small, fill=MUTED, anchor="mm")
        draw.text((col1_x, row1_y + 10), ip,                  font=font_value, fill=TEXT,  anchor="mm")
        draw.text((col2_x, row1_y - 28), "HOSTNAME",          font=font_small, fill=MUTED, anchor="mm")
        draw.text((col2_x, row1_y + 10), f"{hostname}.local", font=font_value, fill=TEXT,  anchor="mm")

        draw.text((cx, row2_y - 28), "ACESSE PELO NAVEGADOR",         font=font_small, fill=MUTED,  anchor="mm")
        draw.text((cx, row2_y + 10), f"http://{hostname}.local:8080", font=font_url,   fill=ACCENT, anchor="mm")

        draw.line([(cx - 300, 560), (cx + 300, 560)], fill=SEP, width=1)

        draw.text((cx, 620), "CÓDIGO DE PAREAMENTO",                         font=font_small, fill=MUTED, anchor="mm")
        draw.text((cx, 710), pairing_code,                                   font=font_code,  fill=WHITE, anchor="mm")
        draw.text((cx, 775), "Use este código para conectar ao servidor central", font=font_small, fill=MUTED, anchor="mm")

        draw.text((cx, H - 60), f"v{version}  ·  {hostname}", font=font_small, fill=(50, 55, 75), anchor="mm")

        STATUS_IMAGE.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(STATUS_IMAGE))
        logger.info("Imagem de status gerada: %s", STATUS_IMAGE)
        return True

    except ImportError:
        logger.warning("Pillow não instalado — tela de status indisponível.")
        return False
    except Exception as e:
        logger.error("Erro ao gerar imagem de status: %s", e)
        return False


class ScreenService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None

    def _kill(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def show_status(
        self,
        display_name: str,
        hostname: str,
        ip: str,
        pairing_code: str,
        version: str = "0.1.0",
    ) -> None:
        """Gera a imagem de status em paisagem (1920×1080) e exibe no HDMI via mpv."""
        self._kill()

        if not _generate_image(display_name, hostname, ip, pairing_code, version):
            logger.warning("Não foi possível exibir tela de status.")
            return

        cmd = [
            "mpv",
            "--fs",
            "--no-border",
            "--no-osc",
            "--no-input-terminal",
            "--vo=drm",
            "--drm-device=/dev/dri/card1",
            "--hwdec=no",
            "--image-display-duration=inf",
            "--loop-file=inf",
            str(STATUS_IMAGE),
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Tela de status exibida no HDMI.")

    def hide(self) -> None:
        """Remove a tela de status."""
        self._kill()
        logger.info("Tela de status removida.")

    def is_showing(self) -> bool:
        return self._process is not None and self._process.poll() is None


    def show_status_from_config(self) -> None:
        """Lê a config atual e exibe a tela de status automaticamente."""
        try:
            from app.config_service import config_service
            cfg = config_service.config
            identity = config_service.identity
            pairing = config_service.pairing
            import subprocess as _sp
            result = _sp.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
            ip = result.stdout.strip().split()[0] if result.stdout.strip() else "N/A"
            self.show_status(
                display_name=cfg.display_name,
                hostname=cfg.hostname,
                ip=ip,
                pairing_code=pairing.pairing_code,
                version=identity.software_version,
            )
        except Exception as e:
            logger.error("Erro ao exibir status automático: %s", e)


screen_service = ScreenService()
