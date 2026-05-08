from __future__ import annotations

import logging
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth_service import auth_service
from app.config_service import config_service
from app import media_service
from app import network_service
from app import reset_service
from app.player_service import player_service
from app.screen_service import screen_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/av-signage/logs/app.log"),
    ],
)
logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path("/opt/av-signage/web/templates")
STATIC_DIR = Path("/opt/av-signage/web/static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_service.promote_pending()
    config_service.load()
    player_service.set_screen_service(screen_service)
    screen_service.show_status_from_config()
    logger.info("AV Signage Player iniciado.")
    yield
    player_service.stop(show_status=False)
    screen_service.hide()


app = FastAPI(title="AV Signage Player", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session_token(request: Request) -> Optional[str]:
    return request.cookies.get("session")


def is_authenticated(request: Request) -> bool:
    return auth_service.validate_session(get_session_token(request))


def require_auth(request: Request) -> Optional[RedirectResponse]:
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return None


def require_auth_json(request: Request) -> Optional[JSONResponse]:
    if not is_authenticated(request):
        return JSONResponse({"error": "Não autenticado"}, status_code=401)
    return None


def _get_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}h {minutes}m {secs}s"
    except Exception:
        return "N/A"


def _get_cpu_temp() -> str:
    try:
        temp_raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return f"{int(temp_raw) / 1000:.1f}°C"
    except Exception:
        return "N/A"


def _get_disk_usage() -> dict:
    try:
        usage = shutil.disk_usage("/opt/av-signage")
        return {
            "total_gb": round(usage.total / 1_073_741_824, 1),
            "used_gb": round(usage.used / 1_073_741_824, 1),
            "free_gb": round(usage.free / 1_073_741_824, 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        return {}


def _get_ip() -> str:
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ips = result.stdout.strip().split()
        return ips[0] if ips else "N/A"
    except Exception:
        return "N/A"


def _get_mac() -> str:
    try:
        return Path("/sys/class/net/eth0/address").read_text().strip()
    except Exception:
        return "N/A"


# ---------------------------------------------------------------------------
# Páginas web
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if is_authenticated(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "first_access": auth_service.is_first_access(),
        "error": error,
    })


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(...),
    password_confirm: str = Form(default=""),
):
    client_ip = request.client.host if request.client else "unknown"

    if auth_service.is_locked_out(client_ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "first_access": auth_service.is_first_access(),
            "error": "Muitas tentativas. Aguarde 15 minutos.",
        }, status_code=429)

    if auth_service.is_first_access():
        if len(password) < 6:
            return templates.TemplateResponse("login.html", {
                "request": request, "first_access": True,
                "error": "A senha deve ter no mínimo 6 caracteres.",
            })
        if password != password_confirm:
            return templates.TemplateResponse("login.html", {
                "request": request, "first_access": True,
                "error": "As senhas não conferem.",
            })
        auth_service.set_password(password)
    else:
        if not auth_service.verify_password(password):
            remaining = auth_service.record_failed_attempt(client_ip)
            msg = "Senha incorreta."
            if remaining == 0:
                msg = "Conta bloqueada por 15 minutos."
            elif remaining <= 2:
                msg = f"Senha incorreta. {remaining} tentativa(s) restante(s)."
            return templates.TemplateResponse("login.html", {
                "request": request, "first_access": False, "error": msg,
            }, status_code=401)

    auth_service.clear_attempts(client_ip)
    token = auth_service.create_session()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(key="session", value=token, httponly=True, samesite="lax", max_age=3600)
    return response


@app.post("/logout")
async def logout(request: Request):
    auth_service.invalidate_session(get_session_token(request))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect

    cfg = config_service.config
    identity = config_service.identity
    pairing = config_service.pairing
    ps = player_service.status()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "display_name": cfg.display_name,
        "hostname": cfg.hostname,
        "mdns_url": f"http://{cfg.hostname}.local",
        "ip": _get_ip(),
        "mac": _get_mac(),
        "network_mode": cfg.network.mode,
        "server_configured": cfg.server.enabled,
        "paired": pairing.paired,
        "pairing_code": pairing.pairing_code,
        "manual_mode": cfg.manual_mode,
        "last_media": cfg.last_media,
        "player_status": "playing" if ps["playing"] else "stopped",
        "current_file": ps["current_file"],
        "loop": ps["loop"],
        "cpu_temp": _get_cpu_temp(),
        "disk": _get_disk_usage(),
        "uptime": _get_uptime(),
        "software_version": identity.software_version,
        "hardware_model": identity.hardware_model,
        "device_id": identity.device_id,
    })


@app.get("/media", response_class=HTMLResponse)
async def media_page(request: Request, msg: str = "", error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect

    files = media_service.list_media()
    ps = player_service.status()

    return templates.TemplateResponse("media.html", {
        "request": request,
        "files": files,
        "player": ps,
        "msg": msg,
        "error": error,
        "free_gb": round(media_service.get_free_bytes() / 1_073_741_824, 1),
    })


# ---------------------------------------------------------------------------
# API — Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    cfg = config_service.config
    identity = config_service.identity
    pairing = config_service.pairing
    ps = player_service.status()

    return JSONResponse({
        "product_name": cfg.product_name,
        "software_version": identity.software_version,
        "device_id": identity.device_id,
        "hardware_model": identity.hardware_model,
        "display_name": cfg.display_name,
        "hostname": cfg.hostname,
        "mdns_url": f"http://{cfg.hostname}.local",
        "ip": _get_ip(),
        "mac": _get_mac(),
        "network_mode": cfg.network.mode,
        "server_configured": cfg.server.enabled,
        "paired": pairing.paired,
        "pairing_code": pairing.pairing_code,
        "manual_mode": cfg.manual_mode,
        "player": ps,
        "cpu_temp": _get_cpu_temp(),
        "disk": _get_disk_usage(),
        "uptime": _get_uptime(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# API — Mídia
# ---------------------------------------------------------------------------

@app.get("/api/media")
async def api_list_media(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    return JSONResponse({"files": media_service.list_media()})


@app.post("/api/media/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    err = require_auth_json(request)
    if err:
        return err

    filename = Path(file.filename).name
    if not filename:
        return JSONResponse({"error": "Nome de arquivo inválido."}, status_code=400)

    if media_service.file_exists(filename):
        return JSONResponse({"error": f"Arquivo '{filename}' já existe. Remova antes de enviar novamente."}, status_code=409)

    try:
        data = await file.read()
        media_service.save_upload(filename, data)
        return JSONResponse({"ok": True, "filename": filename, "size_mb": round(len(data) / 1_048_576, 1)})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/media/{filename}")
async def api_delete_media(request: Request, filename: str):
    err = require_auth_json(request)
    if err:
        return err

    if player_service.is_playing() and player_service.status()["current_file"] == filename:
        player_service.stop()

    try:
        media_service.delete_media(filename)
        return JSONResponse({"ok": True})
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# API — Player
# ---------------------------------------------------------------------------

@app.post("/api/player/play")
async def api_play(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    body = await request.json()
    filename = body.get("filename", "")
    loop = body.get("loop", None)

    if not filename:
        return JSONResponse({"error": "filename obrigatório."}, status_code=400)

    try:
        player_service.play(filename, loop=loop)
        config_service.update_last_media(filename)
        return JSONResponse({"ok": True, **player_service.status()})
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/player/stop")
async def api_stop(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    player_service.stop()
    return JSONResponse({"ok": True, **player_service.status()})


@app.post("/api/player/restart")
async def api_restart(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    try:
        player_service.restart()
        return JSONResponse({"ok": True, **player_service.status()})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/player/loop")
async def api_loop(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    enabled = body.get("enabled", True)
    player_service.set_loop(bool(enabled))
    return JSONResponse({"ok": True, **player_service.status()})


# ---------------------------------------------------------------------------
# Páginas — Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str = "", error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "msg": msg,
        "error": error,
    })


@app.get("/network", response_class=HTMLResponse)
async def network_page(request: Request, msg: str = "", error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect

    cfg = config_service.config
    net_info = network_service.get_current_network_info()

    return templates.TemplateResponse("network.html", {
        "request": request,
        "msg": msg,
        "error": error,
        "display_name": cfg.display_name,
        "device_name": cfg.device_name,
        "hostname": cfg.hostname,
        "network_mode": cfg.network.mode,
        "ip": cfg.network.ip,
        "mask": cfg.network.mask,
        "gateway": cfg.network.gateway,
        "dns": cfg.network.dns,
        "net_info": net_info,
    })


# ---------------------------------------------------------------------------
# API — Sistema
# ---------------------------------------------------------------------------

@app.post("/api/system/reboot")
async def api_reboot(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    reset_service.reboot()
    return JSONResponse({"ok": True, "message": "Reiniciando..."})


@app.post("/api/system/reset")
async def api_reset(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    if body.get("confirm") != "RESET":
        return JSONResponse({"error": "Confirmação inválida. Envie confirm='RESET'."}, status_code=400)
    result = reset_service.reset_config(stop_player_fn=player_service.stop)
    auth_service.reset_password()
    config_service.load()
    return JSONResponse(result)


@app.post("/api/system/factory")
async def api_factory(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    if body.get("confirm") != "FACTORY":
        return JSONResponse({"error": "Confirmação inválida. Envie confirm='FACTORY'."}, status_code=400)
    result = reset_service.factory_reset(stop_player_fn=player_service.stop)
    auth_service.reset_password()
    config_service.load()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# API — Rede e nome
# ---------------------------------------------------------------------------

@app.post("/api/network/apply")
async def api_network_apply(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    body = await request.json()
    mode = body.get("mode", "dhcp")
    display_name = body.get("display_name", "").strip()
    device_name = body.get("device_name", "").strip()

    from app.models import UserConfig, NetworkConfig

    # Validar e construir hostname
    hostname = ""
    if device_name:
        hostname = network_service.build_hostname(device_name)
        ok, msg = network_service.validate_hostname(hostname)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)

    # Validar rede estática
    if mode == "static":
        ip = body.get("ip", "").strip()
        mask = body.get("mask", "").strip()
        gateway = body.get("gateway", "").strip()
        dns_raw = body.get("dns", [])
        dns = [d.strip() for d in dns_raw if d.strip()]
        ok, msg = network_service.validate_static_config(ip, mask, gateway, dns)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        net_cfg = NetworkConfig(mode="static", ip=ip, mask=mask, gateway=gateway, dns=dns)
    else:
        net_cfg = NetworkConfig(mode="dhcp")

    # Salvar pending_config
    from app.config_service import _load_json, USER_CONFIG_FILE
    user_data = _load_json(USER_CONFIG_FILE)
    user_data["network"] = net_cfg.model_dump()
    if display_name:
        user_data["display_name"] = display_name
    if device_name:
        user_data["device_name"] = device_name
        user_data["hostname"] = hostname
    config_service.save_pending(UserConfig(**user_data))

    # Aplicar hostname imediatamente (não precisa de reboot)
    if hostname:
        ok, msg = network_service.apply_hostname(hostname)
        if not ok:
            logger.warning("Hostname não aplicado: %s", msg)

    # Aplicar rede
    if mode == "static":
        ok, msg = network_service.apply_static(ip, mask, gateway, dns)
    else:
        ok, msg = network_service.apply_dhcp()

    if not ok:
        return JSONResponse({"error": f"Erro ao aplicar rede: {msg}"}, status_code=500)

    # Promover pending para user_config
    config_service.promote_pending()

    return JSONResponse({
        "ok": True,
        "hostname": hostname or config_service.config.hostname,
        "mode": mode,
        "reboot_required": mode == "static",
    })


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)
