from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.auth_service import auth_service
from app.services.config_service import config_service
from app.services import media_service
from app.services import network_service
from app.services import reset_service
from app.services import device_service
from app.services import playlist_service
from app.services import schedule_service
from app.services.player_service import player_service
from app.services.display_service import display_service

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

_schedule_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# ScheduleRunner — verifica agendamentos a cada 60s
# ---------------------------------------------------------------------------

async def _schedule_runner() -> None:
    """Background task: verifica e executa agendamentos locais."""
    logger.info("ScheduleRunner iniciado.")
    while True:
        try:
            await asyncio.sleep(60)
            _check_schedule()
        except asyncio.CancelledError:
            logger.info("ScheduleRunner encerrado.")
            break
        except Exception as e:
            logger.error("ScheduleRunner erro: %s", e)


def _check_schedule() -> None:
    """Lógica de verificação de agendamento. Chamada pelo runner e na inicialização."""
    state = config_service.state
    if state.manual_override:
        return

    active = schedule_service.get_active_schedule()
    if active:
        playlist_id = active["playlist_id"]
        ps = player_service.status()
        # Só troca se não está já tocando esta playlist
        if not ps.get("playing_playlist") or ps.get("playlist_id") != playlist_id:
            pl = playlist_service.get_playlist(playlist_id)
            if pl and pl.get("items"):
                player_service.play_playlist(
                    playlist_id=pl["id"],
                    items=pl["items"],
                    loop=pl.get("loop", True),
                    stinger=pl.get("stinger", ""),
                    stinger_duration=pl.get("stinger_duration", 1),
                    name=pl.get("name", ""),
                )
                config_service.save_state(
                    active_schedule_id=active["id"],
                    last_playlist=playlist_id,
                )
                logger.info("Agendamento ativo: %s → playlist: %s", active["name"], playlist_id)
        return

    # Sem agenda ativa — checar playlist padrão
    fallback_id = schedule_service.get_fallback_playlist_id()
    if fallback_id:
        ps = player_service.status()
        if not ps.get("playing"):
            pl = playlist_service.get_playlist(fallback_id)
            if pl and pl.get("items"):
                player_service.play_playlist(
                    playlist_id=pl["id"],
                    items=pl["items"],
                    loop=pl.get("loop", True),
                    stinger=pl.get("stinger", ""),
                    stinger_duration=pl.get("stinger_duration", 1),
                    name=pl.get("name", ""),
                )
                config_service.save_state(last_playlist=fallback_id, active_schedule_id="")
                logger.info("Playlist padrão iniciada: %s", fallback_id)
    else:
        if not player_service.is_playing():
            player_service.play_standby()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _schedule_task
    config_service.promote_pending()
    config_service.load()
    player_service.set_screen_service(display_service)

    # Verificar agendamento imediatamente ao iniciar
    await asyncio.sleep(0)
    _check_schedule()
    # play_standby já é chamado por _check_schedule quando não há conteúdo agendado

    _schedule_task = asyncio.create_task(_schedule_runner())
    logger.info("AV Signage Player v0.2 iniciado.")
    yield
    if _schedule_task:
        _schedule_task.cancel()
    player_service.stop(show_status=False)


app = FastAPI(title="AV Signage Player", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Auth helpers
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


# ---------------------------------------------------------------------------
# Páginas web — Auth
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


# ---------------------------------------------------------------------------
# Páginas web — Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect

    cfg = config_service.config
    identity = config_service.identity
    pairing = config_service.pairing
    ps = player_service.status()
    dev = device_service.get_device_info()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "display_name": cfg.display_name,
        "hostname": cfg.hostname,
        "mdns_url": f"http://{cfg.hostname}.local",
        "ip": dev["network"]["ip"] or "N/A",
        "mac": dev["network"]["ethernet_mac"] or "N/A",
        "wifi_mac": dev["network"]["wifi_mac"] or "N/A",
        "active_type": dev["network"]["active_type"],
        "network_mode": cfg.network.mode,
        "server_configured": cfg.server.enabled,
        "paired": pairing.paired,
        "pairing_code": pairing.pairing_code,
        "manual_mode": cfg.manual_mode,
        "last_media": cfg.last_media,
        "player_status": "playing" if ps["playing"] else "stopped",
        "current_file": ps["current_file"],
        "loop": ps["loop"],
        "playing_playlist": ps.get("playing_playlist", False),
        "playlist_id": ps.get("playlist_id", ""),
        "cpu_temp": _get_cpu_temp(),
        "disk": _get_disk_usage(),
        "uptime": _get_uptime(),
        "software_version": identity.software_version,
        "hardware_model": identity.hardware_model,
        "device_id": identity.device_id,
        "model": dev["model"],
        "hardware_profile": dev["hardware_profile"],
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


@app.get("/playlists", response_class=HTMLResponse)
async def playlists_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect

    files = media_service.list_media()
    playlists = playlist_service.list_playlists()
    ps = player_service.status()

    return templates.TemplateResponse("playlists.html", {
        "request": request,
        "files": files,
        "playlists": playlists,
        "player": ps,
    })


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect

    schedules   = schedule_service.list_schedules()
    playlists   = playlist_service.list_playlists()
    fallback_id = schedule_service.get_fallback_playlist_id()
    files       = media_service.list_media()
    bg_media    = config_service.config.bg_media

    return templates.TemplateResponse("schedule.html", {
        "request":            request,
        "schedules":          schedules,
        "playlists":          playlists,
        "fallback_playlist_id": fallback_id,
        "files":              files,
        "bg_media":           bg_media,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str = "", error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "msg": msg,
        "error": error,
        "display_rotation": config_service.config.player.display_rotation,
    })


@app.get("/network", response_class=HTMLResponse)
async def network_page(request: Request, msg: str = "", error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect

    cfg = config_service.config
    net_info = network_service.get_current_network_info()
    wifi_info = network_service.get_wifi_info()

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
        "wifi_info": wifi_info,
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
    dev = device_service.get_device_info()

    return JSONResponse({
        "product_name": cfg.product_name,
        "software_version": identity.software_version,
        "device_id": identity.device_id,
        "hardware_model": identity.hardware_model,
        "display_name": cfg.display_name,
        "hostname": cfg.hostname,
        "mdns_url": f"http://{cfg.hostname}.local",
        "ip": dev["network"]["ip"],
        "mac": dev["network"]["ethernet_mac"],
        "active_connection_type": dev["network"]["active_type"],
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
# API — Device
# ---------------------------------------------------------------------------

@app.get("/api/device")
async def api_get_device(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    identity = config_service.identity
    info = device_service.get_device_info()
    info["uptime"] = _get_uptime()
    info["cpu_temp"] = _get_cpu_temp()
    info["disk"] = _get_disk_usage()
    info["os_version"] = device_service.get_os_version()

    return JSONResponse(info)


# ---------------------------------------------------------------------------
# API — Rede
# ---------------------------------------------------------------------------

@app.get("/api/network")
async def api_get_network(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    cfg = config_service.config
    net_info = network_service.get_current_network_info()
    wifi_info = network_service.get_wifi_info()
    dev_net = device_service.get_device_info()["network"]

    return JSONResponse({
        "mode": cfg.network.mode,
        "ip": dev_net["ip"],
        "ethernet_mac": dev_net["ethernet_mac"],
        "wifi_mac": dev_net["wifi_mac"],
        "active_type": dev_net["active_type"],
        "current": net_info,
        "wifi": wifi_info,
    })


@app.get("/api/network/wifi/scan")
async def api_wifi_scan(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    networks = network_service.scan_wifi()
    return JSONResponse({"networks": networks})


@app.post("/api/network/wifi/connect")
async def api_wifi_connect(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    body = await request.json()
    ssid = body.get("ssid", "").strip()
    password = body.get("password", "")

    if not ssid:
        return JSONResponse({"error": "SSID obrigatório."}, status_code=400)

    ok, msg = network_service.connect_wifi(ssid, password)
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    return JSONResponse({"ok": True, "ssid": ssid})


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

    hostname = ""
    if device_name:
        hostname = network_service.build_hostname(device_name)
        ok, msg = network_service.validate_hostname(hostname)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)

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

    from app.services.config_service import _load_json, USER_CONFIG_FILE
    user_data = _load_json(USER_CONFIG_FILE)
    user_data["network"] = net_cfg.model_dump()
    if display_name:
        user_data["display_name"] = display_name
    if device_name:
        user_data["device_name"] = device_name
        user_data["hostname"] = hostname
    config_service.save_pending(UserConfig(**user_data))

    if hostname:
        ok, msg = network_service.apply_hostname(hostname)
        if not ok:
            logger.warning("Hostname não aplicado: %s", msg)

    if mode == "static":
        ok, msg = network_service.apply_static(ip, mask, gateway, dns)
    else:
        ok, msg = network_service.apply_dhcp()

    if not ok:
        return JSONResponse({"error": f"Erro ao aplicar rede: {msg}"}, status_code=500)

    config_service.promote_pending()
    asyncio.get_event_loop().call_later(2, reset_service.reboot)

    return JSONResponse({
        "ok": True,
        "hostname": hostname or config_service.config.hostname,
        "mode": mode,
        "reboot_required": True,
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
        return JSONResponse({"error": f"Arquivo '{filename}' já existe."}, status_code=409)

    try:
        data = await file.read()
        media_service.save_upload(filename, data)
        ftype = media_service.detect_type(filename)
        return JSONResponse({
            "ok": True,
            "filename": filename,
            "type": ftype,
            "size_mb": round(len(data) / 1_048_576, 1),
        })
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
    duration = int(body.get("duration_seconds", 10))

    if not filename:
        return JSONResponse({"error": "filename obrigatório."}, status_code=400)

    try:
        player_service.play(filename, loop=loop, duration_seconds=duration)
        config_service.update_last_media(filename)
        config_service.save_state(manual_override=True)
        return JSONResponse({"ok": True, **player_service.status()})
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/player/stop")
async def api_stop(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    player_service.stop()
    config_service.save_state(manual_override=False)
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


@app.post("/api/player/next")
async def api_player_next(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    try:
        player_service.next_item()
        return JSONResponse({"ok": True, **player_service.status()})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/player/previous")
async def api_player_previous(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    try:
        player_service.previous_item()
        return JSONResponse({"ok": True, **player_service.status()})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/player/rotation")
async def api_rotation(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    rotation = body.get("rotation", "normal")
    if rotation not in ("normal", "left", "right"):
        return JSONResponse({"error": "Valor inválido. Use: normal, left, right"}, status_code=400)

    from app.models import PlayerConfig
    user = config_service._user
    current_player = user.player if user.player is not None else config_service._defaults.player
    updated_player = current_player.model_copy(update={"display_rotation": rotation})
    user.player = updated_player
    config_service.save_user_config(user)

    if player_service.is_playing():
        player_service.play(player_service.status()["current_file"])
    else:
        display_service.show_status_from_config()

    return JSONResponse({"ok": True, "rotation": rotation})


# ---------------------------------------------------------------------------
# API — Display/Orientação
# ---------------------------------------------------------------------------

_ORIENTATION_MAP = {
    "landscape": "normal",
    "portrait": "right",
    "landscape-flipped": "normal",
    "portrait-flipped": "left",
}


@app.post("/api/display/orientation")
async def api_display_orientation(request: Request):
    err = require_auth_json(request)
    if err:
        return err

    body = await request.json()
    orientation = body.get("orientation", "landscape")
    rotation = _ORIENTATION_MAP.get(orientation)
    if rotation is None:
        rotation = body.get("rotation")
    if rotation not in ("normal", "left", "right"):
        return JSONResponse({
            "error": "Orientação inválida. Use: landscape, portrait, portrait-flipped, landscape-flipped"
        }, status_code=400)

    from app.models import PlayerConfig
    user = config_service._user
    current_player = user.player if user.player is not None else config_service._defaults.player
    user.player = current_player.model_copy(update={"display_rotation": rotation})
    config_service.save_user_config(user)

    if player_service.is_playing():
        player_service.play(player_service.status()["current_file"])
    else:
        display_service.show_status_from_config()

    return JSONResponse({"ok": True, "orientation": orientation, "rotation": rotation})


# ---------------------------------------------------------------------------
# API — Playlists
# ---------------------------------------------------------------------------

@app.get("/api/playlists")
async def api_list_playlists(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    return JSONResponse({"playlists": playlist_service.list_playlists()})


@app.post("/api/playlists")
async def api_create_playlist(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    try:
        body = await request.json()
        pl = playlist_service.create_playlist(body)
        return JSONResponse({"ok": True, "playlist": pl})
    except (ValueError, KeyError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/playlists/{playlist_id}")
async def api_get_playlist(request: Request, playlist_id: str):
    err = require_auth_json(request)
    if err:
        return err
    pl = playlist_service.get_playlist(playlist_id)
    if pl is None:
        return JSONResponse({"error": "Playlist não encontrada."}, status_code=404)
    return JSONResponse(pl)


@app.put("/api/playlists/{playlist_id}")
async def api_update_playlist(request: Request, playlist_id: str):
    err = require_auth_json(request)
    if err:
        return err
    try:
        body = await request.json()
        pl = playlist_service.update_playlist(playlist_id, body)
        return JSONResponse({"ok": True, "playlist": pl})
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/playlists/{playlist_id}")
async def api_delete_playlist(request: Request, playlist_id: str):
    err = require_auth_json(request)
    if err:
        return err
    try:
        playlist_service.delete_playlist(playlist_id)
        return JSONResponse({"ok": True})
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/playlists/{playlist_id}/play")
async def api_play_playlist(request: Request, playlist_id: str):
    err = require_auth_json(request)
    if err:
        return err

    pl = playlist_service.get_playlist(playlist_id)
    if pl is None:
        return JSONResponse({"error": "Playlist não encontrada."}, status_code=404)
    if not pl.get("items"):
        return JSONResponse({"error": "Playlist sem itens."}, status_code=400)

    player_service.play_playlist(
        playlist_id=pl["id"],
        items=pl["items"],
        loop=pl.get("loop", True),
        stinger=pl.get("stinger", ""),
        stinger_duration=pl.get("stinger_duration", 1),
        name=pl.get("name", ""),
    )
    config_service.save_state(manual_override=True, last_playlist=playlist_id)
    return JSONResponse({"ok": True, **player_service.status()})


@app.get("/api/player/standby")
async def api_get_standby(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    return JSONResponse({"bg_media": config_service.config.bg_media})


@app.post("/api/player/standby")
async def api_set_standby(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    bg = body.get("bg_media", "")
    if bg and not (Path("/opt/av-signage/media/local") / bg).exists():
        return JSONResponse({"error": "Arquivo não encontrado."}, status_code=400)
    config_service.save_bg_media(bg)
    return JSONResponse({"ok": True, "bg_media": bg})


# ---------------------------------------------------------------------------
# API — Agendamentos
# ---------------------------------------------------------------------------

@app.get("/api/schedules")
async def api_list_schedules(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    return JSONResponse({
        "schedules": schedule_service.list_schedules(),
        "fallback_playlist_id": schedule_service.get_fallback_playlist_id(),
    })


@app.get("/api/schedules/active")
async def api_active_schedule(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    active = schedule_service.get_active_schedule()
    return JSONResponse({"active": active})


@app.post("/api/schedules")
async def api_create_schedule(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    try:
        body = await request.json()
        sc = schedule_service.create_schedule(body)
        return JSONResponse({"ok": True, "schedule": sc})
    except (ValueError, KeyError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/schedules/{schedule_id}")
async def api_get_schedule(request: Request, schedule_id: str):
    err = require_auth_json(request)
    if err:
        return err
    sc = schedule_service.get_schedule(schedule_id)
    if sc is None:
        return JSONResponse({"error": "Agendamento não encontrado."}, status_code=404)
    return JSONResponse(sc)


@app.put("/api/schedules/{schedule_id}")
async def api_update_schedule(request: Request, schedule_id: str):
    err = require_auth_json(request)
    if err:
        return err
    try:
        body = await request.json()
        sc = schedule_service.update_schedule(schedule_id, body)
        return JSONResponse({"ok": True, "schedule": sc})
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/schedules/{schedule_id}")
async def api_delete_schedule(request: Request, schedule_id: str):
    err = require_auth_json(request)
    if err:
        return err
    try:
        schedule_service.delete_schedule(schedule_id)
        return JSONResponse({"ok": True})
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/schedules/fallback")
async def api_set_fallback(request: Request):
    err = require_auth_json(request)
    if err:
        return err
    body = await request.json()
    playlist_id = body.get("playlist_id", "")
    schedule_service.set_fallback_playlist_id(playlist_id)
    return JSONResponse({"ok": True, "fallback_playlist_id": playlist_id})


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
    display_service.show_status_from_config()
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
    display_service.show_status_from_config()
    return JSONResponse(result)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": "0.2.0"})


# ---------------------------------------------------------------------------
# Helpers (local ao main — diagnóstico)
# ---------------------------------------------------------------------------

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
    import shutil
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


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)
