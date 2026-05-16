from __future__ import annotations

import io
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_IMAGE        = Path("/opt/av-signage/media/cache/status_screen.png")
DEFAULT_BG_LANDSCAPE = Path("/opt/av-signage/media/cache/default_bg_landscape.png")
DEFAULT_BG_PORTRAIT  = Path("/opt/av-signage/media/cache/default_bg_portrait.png")
DEFAULT_BG           = DEFAULT_BG_LANDSCAPE  # compatibilidade retroativa
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

ROTATION_DEGREES = {"normal": 0, "right": 90, "left": 270}


def _get_rotation() -> int:
    try:
        from app.services.config_service import config_service
        r = config_service.config.player.display_rotation
        return ROTATION_DEGREES.get(r, 0)
    except Exception:
        return 0


def _make_qr_image(url: str, size: int = 200):
    """Gera imagem PIL do QR Code. Retorna None se qrcode não instalado."""
    try:
        import qrcode
        from PIL import Image

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="white", back_color=(10, 12, 20))
        return qr_img.resize((size, size), resample=0)
    except Exception as e:
        logger.warning("Não foi possível gerar QR Code: %s", e)
        return None


def _generate_image(
    display_name: str,
    hostname: str,
    ip: str,
    pairing_code: str,
    version: str,
    rotation: int = 0,
    fallback_mode: bool = False,
) -> bool:
    """Gera PNG de status com QR Code na orientação correta. Retorna True se sucesso."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        portrait = rotation in (90, 270)
        W, H = (1080, 1920) if portrait else (1920, 1080)

        BG = (10, 12, 20)
        ACCENT = (79, 124, 255)
        TEXT = (232, 234, 240)
        MUTED = (107, 114, 128)
        WHITE = (255, 255, 255)
        SEP = (45, 50, 80)
        WARN = (245, 158, 11)

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                return ImageFont.load_default()

        cx = W // 2
        mdns_url = f"http://{hostname}.local:8080"
        web_url  = f"http://{ip}:8080" if ip and ip != "N/A" else mdns_url

        # QR Code aponta sempre para o endereço mDNS (mais estável e profissional)
        qr_size = 220 if portrait else 200
        qr_img = _make_qr_image(mdns_url, size=qr_size)

        if portrait:
            font_title = load_font(FONT_BOLD, 52)
            font_label = load_font(FONT_PATH, 24)
            font_value = load_font(FONT_BOLD, 32)
            font_small = load_font(FONT_PATH, 20)
            font_code = load_font(FONT_BOLD, 70)
            font_url = load_font(FONT_PATH, 26)

            if fallback_mode:
                draw.text((cx, 200), "AV Signage Player", font=font_title, fill=ACCENT, anchor="mm")
                draw.text((cx, 290), "Modo instalação ativo", font=font_label, fill=WARN, anchor="mm")
                draw.line([(cx - 280, 340), (cx + 280, 340)], fill=SEP, width=1)
                draw.text((cx, 420), "Configure seu notebook:", font=font_label, fill=TEXT, anchor="mm")
                draw.text((cx, 490), "IP: 192.168.50.100", font=font_value, fill=WHITE, anchor="mm")
                draw.text((cx, 560), "Máscara: 255.255.255.0", font=font_value, fill=WHITE, anchor="mm")
                draw.line([(cx - 280, 620), (cx + 280, 620)], fill=SEP, width=1)
                draw.text((cx, 700), "Acesse:", font=font_label, fill=MUTED, anchor="mm")
                draw.text((cx, 760), "http://192.168.50.10:8080", font=font_url, fill=ACCENT, anchor="mm")
                qr_fallback = _make_qr_image("http://192.168.50.10:8080", size=qr_size)
                if qr_fallback:
                    img.paste(qr_fallback, (cx - qr_size // 2, 860))
            else:
                draw.text((cx, 200), "AV Signage Player", font=font_title, fill=ACCENT, anchor="mm")
                draw.text((cx, 285), display_name, font=font_label, fill=MUTED, anchor="mm")
                draw.line([(cx - 280, 335), (cx + 280, 335)], fill=SEP, width=1)

                draw.text((cx, 400), "ENDEREÇO IP", font=font_small, fill=MUTED, anchor="mm")
                draw.text((cx, 445), ip if ip and ip != "N/A" else "Obtendo IP...", font=font_value, fill=TEXT, anchor="mm")

                draw.text((cx, 520), "ACESSE PELO NAVEGADOR", font=font_small, fill=MUTED, anchor="mm")
                draw.text((cx, 565), mdns_url, font=font_url, fill=ACCENT, anchor="mm")
                draw.text((cx, 615), web_url,  font=font_small, fill=MUTED,  anchor="mm")

                if qr_img:
                    qr_y = 670
                    img.paste(qr_img, (cx - qr_size // 2, qr_y))
                    draw.text((cx, qr_y + qr_size + 28), "Aponte a câmera  ·  mesma rede", font=font_small, fill=MUTED, anchor="mm")

                code_y = 1050 if qr_img else 700
                draw.line([(cx - 280, code_y), (cx + 280, code_y)], fill=SEP, width=1)
                draw.text((cx, code_y + 60), "CÓDIGO DE PAREAMENTO", font=font_small, fill=MUTED, anchor="mm")
                draw.text((cx, code_y + 140), pairing_code, font=font_code, fill=WHITE, anchor="mm")

            draw.text((cx, H - 65), f"v{version}  ·  {hostname}", font=font_small, fill=(50, 55, 75), anchor="mm")

        else:
            font_title = load_font(FONT_BOLD, 56)
            font_label = load_font(FONT_PATH, 26)
            font_value = load_font(FONT_BOLD, 32)
            font_small = load_font(FONT_PATH, 22)
            font_code = load_font(FONT_BOLD, 72)
            font_url = load_font(FONT_PATH, 28)

            if fallback_mode:
                draw.text((cx, 130), "AV Signage Player", font=font_title, fill=ACCENT, anchor="mm")
                draw.text((cx, 210), "Modo instalação ativo", font=font_label, fill=WARN, anchor="mm")
                draw.line([(cx - 300, 255), (cx + 300, 255)], fill=SEP, width=1)
                draw.text((W // 4, 340), "Configure seu notebook:", font=font_label, fill=TEXT, anchor="mm")
                draw.text((W // 4, 390), "IP: 192.168.50.100", font=font_value, fill=WHITE, anchor="mm")
                draw.text((W // 4, 440), "Máscara: 255.255.255.0", font=font_value, fill=WHITE, anchor="mm")
                draw.text((W // 4, 530), "Acesse:", font=font_label, fill=MUTED, anchor="mm")
                draw.text((W // 4, 580), "http://192.168.50.10:8080", font=font_url, fill=ACCENT, anchor="mm")
                qr_fallback = _make_qr_image("http://192.168.50.10:8080", size=qr_size)
                if qr_fallback:
                    img.paste(qr_fallback, (W * 3 // 4 - qr_size // 2, 320))
            else:
                draw.text((cx, 130), "AV Signage Player", font=font_title, fill=ACCENT, anchor="mm")
                draw.text((cx, 210), display_name, font=font_label, fill=MUTED, anchor="mm")
                draw.line([(cx - 300, 255), (cx + 300, 255)], fill=SEP, width=1)

                col1_x = W // 4
                col2_x = W * 3 // 4

                # Coluna esquerda: info
                draw.text((col1_x, 320), "ENDEREÇO IP", font=font_small, fill=MUTED, anchor="mm")
                draw.text((col1_x, 360), ip if ip and ip != "N/A" else "...", font=font_value, fill=TEXT, anchor="mm")
                draw.text((col1_x, 430), "HOSTNAME", font=font_small, fill=MUTED, anchor="mm")
                draw.text((col1_x, 470), f"{hostname}.local", font=font_value, fill=TEXT, anchor="mm")
                draw.text((col1_x, 540), "NAVEGADOR", font=font_small, fill=MUTED, anchor="mm")
                draw.text((col1_x, 580), mdns_url, font=font_url,   fill=ACCENT, anchor="mm")
                draw.text((col1_x, 620), web_url,  font=font_small, fill=MUTED,  anchor="mm")

                draw.line([(cx - 10, 300), (cx - 10, 750)], fill=SEP, width=1)

                # Coluna direita: QR Code centralizado verticalmente
                if qr_img:
                    # Área disponível: y=255 (divider) até y=780 (divider inferior)
                    # Bloco: QR (200px) + gap + 2 linhas texto (~54px) → ~274px
                    col_top, col_bot = 255, 780
                    block_h = qr_size + 30 + 54
                    qr_y = (col_top + col_bot - block_h) // 2
                    qr_x = col2_x - qr_size // 2
                    img.paste(qr_img, (qr_x, qr_y))
                    txt_y1 = qr_y + qr_size + 22
                    txt_y2 = txt_y1 + 30
                    draw.text((col2_x, txt_y1), "Conecte a mesma rede", font=font_small, fill=MUTED, anchor="mm")
                    draw.text((col2_x, txt_y2), "· ·  Aponte a câmera  · ·", font=font_small, fill=(75, 82, 100), anchor="mm")

                draw.line([(cx - 300, 780), (cx + 300, 780)], fill=SEP, width=1)
                draw.text((cx, 835), "CÓDIGO DE PAREAMENTO", font=font_small, fill=MUTED, anchor="mm")
                draw.text((cx, 910), pairing_code, font=font_code, fill=WHITE, anchor="mm")

            draw.text((cx, H - 55), f"v{version}  ·  {hostname}", font=font_small, fill=(50, 55, 75), anchor="mm")

        STATUS_IMAGE.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(STATUS_IMAGE))
        logger.info("Imagem de status gerada: %dx%d rotation=%d°", W, H, rotation)
        return True

    except ImportError:
        logger.warning("Pillow não instalado — tela de status indisponível.")
        return False
    except Exception as e:
        logger.error("Erro ao gerar imagem de status: %s", e)
        return False


def _generate_default_bg_image(rotation: int = 0, ip: str = "", target: Optional[Path] = None) -> bool:
    """Gera a imagem de standby com a marca Maggiore.AV. Retorna True se sucesso."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        portrait = rotation in (90, 270)
        W, H = (1080, 1920) if portrait else (1920, 1080)

        BG     = (8,   10,  18)
        TEXT   = (220, 225, 235)
        GOLD   = (210, 165, 55)
        MUTED  = (75,  85, 105)
        SEP    = (30,  38,  65)
        ADDR   = (45,  55,  80)

        img  = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)
        cx   = W // 2
        cy   = H // 2

        # Círculos concêntricos decorativos (fundo sutil)
        for r, alpha in [(min(W, H) // 2, SEP), (min(W, H) // 3, SEP), (min(W, H) // 5, (20, 28, 52))]:
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=alpha, width=1)

        def lf(path: str, size: int) -> ImageFont.FreeTypeFont:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                return ImageFont.load_default()

        if portrait:
            f_brand = lf(FONT_BOLD, 110)
            f_av    = lf(FONT_BOLD,  72)
            f_tag   = lf(FONT_PATH,  32)
            f_addr  = lf(FONT_PATH,  24)
            logo_y  = cy - 120
        else:
            f_brand = lf(FONT_BOLD, 128)
            f_av    = lf(FONT_BOLD,  80)
            f_tag   = lf(FONT_PATH,  34)
            f_addr  = lf(FONT_PATH,  24)
            logo_y  = cy - 110

        # "MAGGIORE" em branco
        draw.text((cx, logo_y),        "MAGGIORE",        font=f_brand, fill=TEXT, anchor="mm")
        # ".AV" em dourado
        draw.text((cx, logo_y + 145),  ".AV",             font=f_av,   fill=GOLD, anchor="mm")
        # Linha separadora
        sep_y = logo_y + 205
        draw.line([(cx - 180, sep_y), (cx + 180, sep_y)], fill=SEP, width=1)
        # Tagline
        draw.text((cx, sep_y + 46),    "Digital Signage", font=f_tag,  fill=MUTED, anchor="mm")

        # IP no rodapé (sutil, para acesso inicial)
        if ip:
            draw.text((cx, H - 52), f"Acesse  ·  http://{ip}:8080",
                      font=f_addr, fill=ADDR, anchor="mm")

        out = target or (DEFAULT_BG_PORTRAIT if portrait else DEFAULT_BG_LANDSCAPE)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out))
        logger.info("BG padrão Maggiore.AV gerado: %dx%d rotation=%d° → %s", W, H, rotation, out.name)
        return True

    except ImportError:
        logger.warning("Pillow não instalado — BG padrão indisponível.")
        return False
    except Exception as e:
        logger.error("Erro ao gerar BG padrão: %s", e)
        return False


class DisplayService:
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

    def _launch_mpv(self, rotation: int) -> None:
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
            f"--video-rotate={rotation}",
            str(STATUS_IMAGE),
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def show_status(
        self,
        display_name: str,
        hostname: str,
        ip: str,
        pairing_code: str,
        version: str = "0.2.0",
        fallback_mode: bool = False,
    ) -> None:
        self._kill()
        rotation = _get_rotation()
        if not _generate_image(display_name, hostname, ip, pairing_code, version, rotation, fallback_mode):
            logger.warning("Não foi possível exibir tela de status.")
            return
        self._launch_mpv(rotation)
        logger.info("Tela de status exibida no HDMI (rotation=%d°, fallback=%s).", rotation, fallback_mode)

    def render_status_image(self) -> Optional[Path]:
        """Gera a imagem de status e retorna o path. NÃO inicia mpv."""
        try:
            from app.services.config_service import config_service
            from app.services.device_service import get_ip

            cfg      = config_service.config
            identity = config_service.identity
            pairing  = config_service.pairing
            state    = config_service.state

            ip       = get_ip() or "N/A"
            fallback = (state.network_mode == "fallback")
            rotation = _get_rotation()

            if _generate_image(
                display_name=cfg.display_name,
                hostname=cfg.hostname,
                ip=ip,
                pairing_code=pairing.pairing_code,
                version=identity.software_version,
                rotation=rotation,
                fallback_mode=fallback,
            ):
                return STATUS_IMAGE
        except Exception as e:
            logger.error("Erro ao renderizar imagem de status: %s", e)
        return None

    def generate_default_bg(self) -> Optional[Path]:
        """Gera ambas as versões (paisagem/retrato) do BG padrão e retorna o path adequado à orientação atual."""
        try:
            from app.services.device_service import get_ip
            rotation = _get_rotation()
            ip = get_ip() or ""
            # Gera ambas as versões na inicialização/atualização
            _generate_default_bg_image(rotation=0,  ip=ip, target=DEFAULT_BG_LANDSCAPE)
            _generate_default_bg_image(rotation=90, ip=ip, target=DEFAULT_BG_PORTRAIT)
            # Retorna a versão correspondente à orientação atual
            portrait = rotation in (90, 270)
            return DEFAULT_BG_PORTRAIT if portrait else DEFAULT_BG_LANDSCAPE
        except Exception as e:
            logger.error("Erro ao gerar BG padrão: %s", e)
        return None

    def show_status_from_config(self) -> None:
        """Delega para player_service.play_standby() para exibir via mpv persistente."""
        try:
            from app.services.player_service import player_service
            player_service.play_standby()
        except Exception as e:
            logger.error("Erro ao exibir status automático: %s", e)

    def hide(self) -> None:
        """Mata processo mpv próprio (se houver). Na nova arquitetura é no-op."""
        self._kill()

    def is_showing(self) -> bool:
        return self._process is not None and self._process.poll() is None


display_service = DisplayService()
