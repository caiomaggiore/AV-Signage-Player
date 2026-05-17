from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

HOSTNAME_PREFIX = "signage"
HOSTNAME_MAX_LEN = 63
NM_CONN_NAME = "av-signage-eth"
NM_WIFI_CONN_PREFIX = "av-signage-wifi"

# ---------------------------------------------------------------------------
# Modo AP (hotspot)
# ---------------------------------------------------------------------------

AP_CONN_NAME = "av-signage-hotspot"
AP_IP        = "10.42.0.1"
AP_PASSWORD  = "12345678"
AP_IFACE     = "wlan0"
# Cache pré‑AP ou após scan ao vivo (`/api/network/wifi/scan` POST)
WIFI_SCAN_CACHE_FILE = Path("/opt/av-signage/config/wifi_scan_cache.json")

# Estado da última tentativa STATION (audit, senha omitida nos logs gravados aqui como audit).
WIFI_PENDING_CONNECT_FILE = Path("/opt/av-signage/config/wifi_pending_connect.json")

# Pedido gravado quando o modo AP está ativo: SSID + password para aplicar após reboot /
# assim que wlan deixar de servir só como AP (`apply_wifi_boot_queue_once`).
WIFI_BOOT_ASSOC_FILE = Path("/opt/av-signage/config/wifi_boot_association.json")
WIFI_BOOT_ASSOC_VERSION = 1

# Espera curta após desligar o hotspot antes de pedir ao NM um rescan Wi‑Fi
WIFI_SCAN_AP_DOWN_SETTLE_SEC = 1.25

_wifi_scan_lock = threading.Lock()
_wifi_assoc_lock = threading.Lock()


def _write_wifi_connect_state(payload: dict) -> None:
    """Grava estado JSON sob /opt/av-signage/config (chmod 0600 onde suportado)."""
    WIFI_PENDING_CONNECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = WIFI_PENDING_CONNECT_FILE.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(WIFI_PENDING_CONNECT_FILE)
    try:
        os.chmod(WIFI_PENDING_CONNECT_FILE, 0o600)
    except OSError:
        pass


def read_wifi_pending_connect_public() -> dict:
    """Lê `wifi_pending_connect.json` para diagnóstico (senha não é persistida aqui)."""
    try:
        raw = WIFI_PENDING_CONNECT_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _nm_args_for_audit(nm_args_exec: List[str]) -> List[str]:
    """Cópia dos argumentos `nmcli` com o valor seguinte a ``password`` substituído por ``***``."""
    out = list(nm_args_exec)
    for i in range(len(out) - 1):
        if out[i] == "password":
            out[i + 1] = "***"
            break
    return out


def _write_wifi_boot_queue(payload: dict) -> None:
    WIFI_BOOT_ASSOC_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = WIFI_BOOT_ASSOC_FILE.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(WIFI_BOOT_ASSOC_FILE)
    try:
        os.chmod(WIFI_BOOT_ASSOC_FILE, 0o600)
    except OSError:
        pass


def queue_wifi_association_for_boot(ssid: str, password: str) -> tuple[bool, str]:
    """Grava SSID + password para `nmcli` depois que o modo AP já não ocupar só o rádio (reboot ou AP off)."""
    if not ssid.strip():
        return False, "SSID vazio."
    conn_name = f"{NM_WIFI_CONN_PREFIX}-{_slugify(ssid)[:20]}"
    doc = {
        "version": WIFI_BOOT_ASSOC_VERSION,
        "ssid": ssid.strip(),
        "password": password or "",
        "connection_profile_name": conn_name,
        "ifname": AP_IFACE,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "last_apply_attempt_at": None,
        "last_error": None,
    }
    try:
        _write_wifi_boot_queue(doc)
        logger.info("Wi‑Fi enfileirado para aplicar após reboot / fim AP: ssid=%s", ssid.strip())
        return True, ""
    except Exception as e:
        logger.error("Erro ao enfileirar Wi‑Fi: %s", e)
        return False, str(e)


def clear_wifi_boot_queue_if_matching_ssid(ssid: str) -> None:
    """Remove fila quando ligação imediata já usou o mesmo SSID."""
    t = (ssid or "").strip()
    if not t:
        return
    try:
        if not WIFI_BOOT_ASSOC_FILE.exists():
            return
        doc = json.loads(WIFI_BOOT_ASSOC_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("ssid") == t:
            WIFI_BOOT_ASSOC_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def read_wifi_boot_queue_meta(exclude_secret: bool = True) -> dict:
    """Metadados do pedido gravado para boot (opcional omitir campo password)."""
    try:
        if not WIFI_BOOT_ASSOC_FILE.exists():
            return {}
        data = dict(json.loads(WIFI_BOOT_ASSOC_FILE.read_text(encoding="utf-8")))
        if exclude_secret:
            data.pop("password", None)
        return data
    except Exception:
        return {}


def is_wifi_boot_queue_pending() -> bool:
    """Indica se existe ``wifi_boot_association.json`` com credenciais STA pendentes."""
    return WIFI_BOOT_ASSOC_FILE.exists()


def apply_wifi_boot_queue_once() -> str:
    """Códigos: ``none``, ``connected``, ``failed``, ``error``.

    Enquanto o modo AP ocupa o rádio, faz ``connection down`` no hotspot antes de ligar à STA.
    Se a ligação STA falhar, tenta recolocar o hotspot para não ficar sem acesso de configuração."""
    paused_ap = False
    try:
        if not WIFI_BOOT_ASSOC_FILE.exists():
            return "none"
        try:
            data = dict(json.loads(WIFI_BOOT_ASSOC_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            WIFI_BOOT_ASSOC_FILE.unlink()
            return "error"
        ssid = str(data.get("ssid", "")).strip()
        pwd = str(data.get("password", "") or "")
        if not ssid:
            WIFI_BOOT_ASSOC_FILE.unlink()
            return "error"

        data["last_apply_attempt_at"] = datetime.now(timezone.utc).isoformat()
        data.pop("last_error", None)
        _write_wifi_boot_queue(data)

        ap_was = is_hotspot_active()
        if ap_was:
            ok_down, msg_down = hotspot_connection_down()
            if ok_down:
                paused_ap = True
                logger.info("Fila Wi‑Fi: hotspot pausado para tentar STA (ssid=%s)", ssid)
            else:
                logger.warning("Fila Wi‑Fi: não pausou hotspot (%s); tentativa STA mesmo assim.", msg_down)
            time.sleep(WIFI_SCAN_AP_DOWN_SETTLE_SEC)

        ok, msg = connect_wifi(ssid, pwd)

        if ok:
            try:
                WIFI_BOOT_ASSOC_FILE.unlink()
            except FileNotFoundError:
                pass
            logger.info("Fila wi‑fi aplicada (ssid=%s).", ssid)
            return "connected"

        # Falhou: repor AP para o utilizador voltar à UI pelo hotspot
        if paused_ap:
            ok_up, up_msg = hotspot_connection_up()
            if not ok_up:
                h = socket.gethostname()
                enable_hotspot(get_ap_ssid(h), AP_PASSWORD)
                logger.warning("Reactivação AP após falha STA: hotspot_connection_up falhou (%s); recreate.", up_msg)

        tail = msg[:4096] if isinstance(msg, str) else str(msg)
        try:
            data = dict(json.loads(WIFI_BOOT_ASSOC_FILE.read_text(encoding="utf-8")))
        except Exception:
            data = {
                "ssid": ssid,
                "version": WIFI_BOOT_ASSOC_VERSION,
                "password": pwd,
                "connection_profile_name": f"{NM_WIFI_CONN_PREFIX}-{_slugify(ssid)[:20]}",
                "ifname": AP_IFACE,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
        data["last_error"] = tail
        _write_wifi_boot_queue(data)
        logger.warning("Fila wi-fi falhou ao aplicar ssid=%s: %s", ssid, tail[:500])
        return "failed"
    except Exception as e:
        if paused_ap and not is_hotspot_active():
            try:
                hotspot_connection_up()
            except Exception:
                try:
                    enable_hotspot(get_ap_ssid(socket.gethostname()), AP_PASSWORD)
                except Exception:
                    pass
        logger.error("apply_wifi_boot_queue_once: %s", e)
        return "error"


def get_ap_ssid(hostname: str) -> str:
    """Gera SSID do hotspot a partir do hostname. Ex: 'AV-signage-caio'."""
    return f"AV-{hostname}"[:32]


def _has_wifi_interface() -> bool:
    """Verifica se wlan0 existe no sistema."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                return True
    except Exception:
        pass
    return False


def enable_hotspot(ssid: str, password: str = AP_PASSWORD) -> tuple[bool, str]:
    """Ativa o hotspot Wi-Fi via NetworkManager."""
    if not _has_wifi_interface():
        return False, "Interface Wi-Fi não encontrada."
    try:
        # Remove conexão anterior se existir
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", AP_CONN_NAME],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            [
                "sudo", "nmcli", "device", "wifi", "hotspot",
                "ifname", AP_IFACE,
                "con-name", AP_CONN_NAME,
                "ssid", ssid,
                "password", password,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        logger.info("Hotspot ativado: SSID=%s IP=%s", ssid, AP_IP)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or str(e))
        logger.error("Erro ao ativar hotspot: %s", msg)
        return False, msg
    except Exception as e:
        logger.error("Erro inesperado ao ativar hotspot: %s", e)
        return False, str(e)


def disable_hotspot() -> tuple[bool, str]:
    """Desativa o hotspot Wi-Fi."""
    try:
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", AP_CONN_NAME],
            capture_output=True, text=True, timeout=15, check=True,
        )
        logger.info("Hotspot desativado.")
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or str(e))
        logger.warning("Erro ao desativar hotspot: %s", msg)
        return False, msg
    except Exception as e:
        logger.warning("Erro inesperado ao desativar hotspot: %s", e)
        return False, str(e)


def hotspot_connection_down() -> tuple[bool, str]:
    """Desliga o hotspot sem apagar o perfil NM (libera o rádio temporariamente)."""
    try:
        if not is_hotspot_active():
            return True, ""
        subprocess.run(
            ["sudo", "nmcli", "connection", "down", AP_CONN_NAME],
            capture_output=True, text=True, timeout=20, check=True,
        )
        logger.info("Hotspot pausado (connection down): %s", AP_CONN_NAME)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or str(e)).strip()
        logger.warning("hotspot_connection_down: %s", msg)
        return False, msg


def hotspot_connection_up() -> tuple[bool, str]:
    """Reativa hotspot pelo perfil existente."""
    try:
        subprocess.run(
            ["sudo", "nmcli", "connection", "up", AP_CONN_NAME],
            capture_output=True, text=True, timeout=35, check=True,
        )
        logger.info("Hotspot retomado (connection up): %s", AP_CONN_NAME)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or str(e)).strip()
        logger.warning("hotspot_connection_up: %s", msg)
        return False, msg


def is_hotspot_active() -> bool:
    """Verifica se o hotspot está ativo."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if parts and parts[0] == AP_CONN_NAME:
                return True
    except Exception as e:
        logger.warning("is_hotspot_active error: %s", e)
    return False


def _device_has_ip(device: str) -> bool:
    """Verifica se um device tem IP atribuído."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("IP4.ADDRESS") and ":" in line:
                addr = line.split(":", 1)[-1].strip()
                # Ignora APIPA (169.254.x.x) pois não tem rota real
                if addr and not addr.startswith("169.254."):
                    return True
    except Exception:
        pass
    return False


def is_connected() -> bool:
    """Verifica se o dispositivo tem alguma conexão ativa (eth ou wifi).

    Não exige conectividade com internet (evita falso negativo em redes
    corporativas onde nmcli CONNECTIVITY retorna 'unknown').
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        hotspot_on = is_hotspot_active()
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            dev, dev_type, state = parts[0], parts[1], parts[2]
            if dev_type not in ("ethernet", "wifi"):
                continue
            if state != "connected":
                continue
            # Exclui a interface wlan0 quando ela está servindo como AP
            if dev == AP_IFACE and hotspot_on:
                continue
            if _device_has_ip(dev):
                return True
    except Exception as e:
        logger.warning("is_connected error: %s", e)
    return False


def get_connection_type() -> dict:
    """Retorna dicionário com status de conexão Ethernet e Wi-Fi.

    Returns:
        {
            "ethernet": bool,
            "wifi": bool,
            "wifi_ssid": str,
            "ap_mode": bool,
        }
    """
    result = {"ethernet": False, "wifi": False, "wifi_ssid": "", "ap_mode": False}
    try:
        hotspot_on = is_hotspot_active()
        result["ap_mode"] = hotspot_on

        nm_result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in nm_result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3 or parts[2] != "connected":
                continue
            dev, dev_type = parts[0], parts[1]
            if dev_type == "ethernet" and _device_has_ip(dev):
                result["ethernet"] = True
            elif dev_type == "wifi" and not (dev == AP_IFACE and hotspot_on):
                if _device_has_ip(dev):
                    result["wifi"] = True
                    # Busca SSID
                    wifi_info = get_wifi_info()
                    result["wifi_ssid"] = wifi_info.get("ssid", "")
    except Exception as e:
        logger.warning("get_connection_type error: %s", e)
    return result


# ---------------------------------------------------------------------------
# Validação de rede
# ---------------------------------------------------------------------------

def validate_ip(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return ip not in ("0.0.0.0",)
    except ValueError:
        return False


def validate_mask(mask: str) -> bool:
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{mask}", strict=False)
        return network.prefixlen > 0
    except ValueError:
        return False


def validate_gateway(gateway: str, ip: str, mask: str) -> tuple[bool, str]:
    if not validate_ip(gateway):
        return False, "Gateway inválido."
    try:
        network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        gw = ipaddress.IPv4Address(gateway)
        if gw == network.network_address:
            return False, "Gateway não pode ser o endereço de rede."
        if gw == network.broadcast_address:
            return False, "Gateway não pode ser o endereço de broadcast."
        if gw not in network:
            return False, f"Gateway {gateway} não está na mesma sub-rede que {ip}/{mask}."
        return True, ""
    except ValueError as e:
        return False, str(e)


def validate_dns(dns_list: List[str]) -> tuple[bool, str]:
    for dns in dns_list:
        if dns and not validate_ip(dns):
            return False, f"DNS inválido: {dns}"
    return True, ""


def validate_static_config(ip: str, mask: str, gateway: str, dns: List[str]) -> tuple[bool, str]:
    if not validate_ip(ip):
        return False, f"IP inválido: {ip}"
    if not validate_mask(mask):
        return False, f"Máscara inválida: {mask}"
    ok, msg = validate_gateway(gateway, ip, mask)
    if not ok:
        return False, msg
    ok, msg = validate_dns(dns)
    if not ok:
        return False, msg
    try:
        network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        addr = ipaddress.IPv4Address(ip)
        if addr == network.network_address:
            return False, "IP não pode ser o endereço de rede."
        if addr == network.broadcast_address:
            return False, "IP não pode ser o endereço de broadcast."
    except ValueError as e:
        return False, str(e)
    return True, ""


# ---------------------------------------------------------------------------
# Hostname e mDNS
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


def build_hostname(device_name: str) -> str:
    slug = _slugify(device_name)
    if not slug:
        slug = "player"
    hostname = f"{HOSTNAME_PREFIX}-{slug}"
    return hostname[:HOSTNAME_MAX_LEN]


def validate_hostname(hostname: str) -> tuple[bool, str]:
    if not hostname.startswith(f"{HOSTNAME_PREFIX}-"):
        return False, f"Hostname deve começar com '{HOSTNAME_PREFIX}-'."
    if "_" in hostname:
        return False, "Hostname não pode conter underscore."
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", hostname):
        return False, "Hostname deve conter apenas letras minúsculas, números e hífen."
    return True, ""


def apply_hostname(hostname: str) -> tuple[bool, str]:
    ok, msg = validate_hostname(hostname)
    if not ok:
        return False, msg
    try:
        subprocess.run(
            ["sudo", "hostnamectl", "set-hostname", hostname],
            timeout=10, check=True, capture_output=True,
        )
        hosts = Path("/etc/hosts").read_text()
        lines = []
        replaced = False
        for line in hosts.splitlines():
            if "127.0.1.1" in line:
                lines.append(f"127.0.1.1\t{hostname}")
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.append(f"127.0.1.1\t{hostname}")
        new_hosts = "\n".join(lines) + "\n"
        proc = subprocess.run(
            ["sudo", "tee", "/etc/hosts"],
            input=new_hosts, text=True,
            timeout=10, capture_output=True,
        )
        if proc.returncode != 0:
            logger.warning("Não foi possível atualizar /etc/hosts: %s", proc.stderr)
        logger.info("Hostname aplicado: %s", hostname)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if e.stderr else str(e)
        logger.error("Erro ao aplicar hostname: %s", msg)
        return False, msg
    except Exception as e:
        logger.error("Erro inesperado ao aplicar hostname: %s", e)
        return False, str(e)


# ---------------------------------------------------------------------------
# NetworkManager / nmcli — Ethernet
# ---------------------------------------------------------------------------

def _get_eth_interface() -> str:
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "ethernet" and parts[2] == "connected":
                return parts[0]
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "ethernet":
                return parts[0]
    except Exception as e:
        logger.warning("Não foi possível detectar interface: %s", e)
    return "eth0"


def apply_dhcp(interface: Optional[str] = None) -> tuple[bool, str]:
    iface = interface or _get_eth_interface()
    try:
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", NM_CONN_NAME],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "nmcli", "connection", "add",
             "type", "ethernet", "con-name", NM_CONN_NAME,
             "ifname", iface, "ipv4.method", "auto", "ipv4.link-local", "enabled"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        subprocess.run(
            ["sudo", "nmcli", "connection", "up", NM_CONN_NAME],
            capture_output=True, timeout=10,
        )
        logger.info("DHCP aplicado na interface %s.", iface)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if e.stderr else str(e)
        logger.error("Erro ao aplicar DHCP: %s", msg)
        return False, msg


def apply_static(
    ip: str, mask: str, gateway: str, dns: List[str],
    interface: Optional[str] = None,
) -> tuple[bool, str]:
    ok, msg = validate_static_config(ip, mask, gateway, dns)
    if not ok:
        return False, msg

    iface = interface or _get_eth_interface()
    try:
        network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        prefix = network.prefixlen
        dns_str = " ".join(d for d in dns if d)

        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", NM_CONN_NAME],
            capture_output=True, timeout=10,
        )
        cmd = [
            "sudo", "nmcli", "connection", "add",
            "type", "ethernet", "con-name", NM_CONN_NAME,
            "ifname", iface, "ipv4.method", "manual",
            "ipv4.addresses", f"{ip}/{prefix}", "ipv4.gateway", gateway,
        ]
        if dns_str:
            cmd += ["ipv4.dns", dns_str]

        subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
        subprocess.run(
            ["sudo", "nmcli", "connection", "up", NM_CONN_NAME],
            capture_output=True, timeout=10,
        )
        logger.info("IP estático aplicado: %s/%s via %s", ip, prefix, gateway)
        return True, ""
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if e.stderr else str(e)
        logger.error("Erro ao aplicar IP estático: %s", msg)
        return False, msg


def get_current_network_info() -> dict:
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", _get_eth_interface()],
            capture_output=True, text=True, timeout=5,
        )
        info = {}
        for line in result.stdout.splitlines():
            if "IP4.ADDRESS" in line:
                info["ip_cidr"] = line.split(":", 1)[-1].strip()
            elif "IP4.GATEWAY" in line:
                info["gateway"] = line.split(":", 1)[-1].strip()
            elif "IP4.DNS" in line and "dns" not in info:
                info["dns"] = line.split(":", 1)[-1].strip()
        return info
    except Exception as e:
        logger.warning("Não foi possível obter info de rede: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------

def _wifi_list_nmcli_rescan() -> List[dict]:
    result = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"],
        capture_output=True, text=True, timeout=35,
    )
    seen: set[str] = set()
    networks: List[dict] = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        ssid = parts[0].strip() if parts else ""
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        security = parts[2] if len(parts) > 2 else ""
        networks.append({"ssid": ssid, "signal": signal, "security": security})
    networks.sort(key=lambda x: x["signal"], reverse=True)
    return networks


# ---------------------------------------------------------------------------
# Wi-Fi cache (antes do modo AP e após atualizações)
# ---------------------------------------------------------------------------

def read_wifi_scan_cache() -> dict:
    """Retorna redes gravadas (pré-scan antes do AP ou última atualização bem-sucedida)."""
    empty = {"networks": [], "saved_at": None}
    try:
        if not WIFI_SCAN_CACHE_FILE.exists():
            return dict(empty)
        data = json.loads(WIFI_SCAN_CACHE_FILE.read_text(encoding="utf-8"))
        nets = data.get("networks")
        if not isinstance(nets, list):
            nets = []
        validated: List[dict] = []
        for n in nets:
            if not isinstance(n, dict):
                continue
            sid = str(n.get("ssid", "") or "").strip()
            if not sid:
                continue
            try:
                sig = int(n.get("signal", 0))
            except (TypeError, ValueError):
                sig = 0
            validated.append({"ssid": sid, "signal": sig, "security": str(n.get("security", "") or "")})
        return {"networks": validated, "saved_at": data.get("saved_at")}
    except Exception as e:
        logger.warning("read_wifi_scan_cache error: %s", e)
        return dict(empty)


def save_wifi_scan_cache(networks: List[dict]) -> None:
    """Serializa redes em JSON sob /opt/av-signage/config/."""
    try:
        WIFI_SCAN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "networks": networks,
        }
        WIFI_SCAN_CACHE_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("save_wifi_scan_cache error: %s", e)


def prefetch_wifi_survey_before_hotspot() -> List[dict]:
    """Varredura em modo estação (sem AP), antes de ativar hotspot; persiste resultado."""
    networks: List[dict] = []
    with _wifi_scan_lock:
        try:
            networks = _wifi_list_nmcli_rescan()
            save_wifi_scan_cache(networks)
            logger.info("Pré-scan Wi‑Fi antes do modo AP: %s redes gravadas.", len(networks))
        except Exception as e:
            logger.warning("prefetch_wifi_survey_before_hotspot error: %s", e)
            networks = []
    return networks


def scan_wifi_live() -> List[dict]:
    """Varredura ao vivo. Com hotspot ativo, suspende o AP (`connection down`), varre e volta.
    Grava sempre o resultado bem-sucedido no cache JSON. Chamadas serializadas.
    """
    with _wifi_scan_lock:
        ap_was_active = False
        networks: List[dict] = []
        try:
            ap_was_active = is_hotspot_active()
            if ap_was_active:
                ok_down, msg_down = hotspot_connection_down()
                if not ok_down:
                    logger.warning(
                        "scan_wifi_live: não foi possível pausar o hotspot (%s); tentando assim mesmo",
                        msg_down,
                    )
                time.sleep(WIFI_SCAN_AP_DOWN_SETTLE_SEC)
            networks = _wifi_list_nmcli_rescan()
            save_wifi_scan_cache(networks)
            return networks
        except Exception as e:
            logger.warning("scan_wifi_live error: %s", e)
            return []
        finally:
            if ap_was_active:
                ok_up, msg_up = hotspot_connection_up()
                if not ok_up:
                    logger.warning(
                        "scan_wifi_live: retomada via `connection up` falhou (%s); recriando hotspot",
                        msg_up,
                    )
                    ssid = get_ap_ssid(socket.gethostname())
                    ok_en, msg_en = enable_hotspot(ssid, AP_PASSWORD)
                    if not ok_en:
                        logger.error("scan_wifi_live: falha ao recriar modo AP depois da varredura: %s", msg_en)


def connect_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Liga Wi‑Fi STA via ``sudo nmcli`` (permissões NOPASSWD na instalação típica).

    Auditoria em ``wifi_pending_connect.json``. Serializado com ``_wifi_assoc_lock``.
    """
    with _wifi_assoc_lock:
        return _wifi_station_nmcli_connect(ssid, password)


def _wifi_station_nmcli_connect(ssid: str, password: str) -> tuple[bool, str]:
    """Executa ``sudo nmcli device wifi connect`` — o sudoers da instalação permite nmcli NOPASSWD."""
    conn_name = f"{NM_WIFI_CONN_PREFIX}-{_slugify(ssid)[:20]}"
    iso = datetime.now(timezone.utc).isoformat()

    nm_args = ["device", "wifi", "connect", ssid, "ifname", AP_IFACE]
    if password:
        nm_args += ["password", password]
    nm_args += ["name", conn_name]

    snapshot_attempt = {
        "ssid": ssid,
        "connection_profile_name": conn_name,
        "ifname": AP_IFACE,
        "requested_at": iso,
        "nm_cli_command": ["sudo", "nmcli", *_nm_args_for_audit(nm_args)],
        "status": "attempting",
    }

    combo = ""

    try:
        _write_wifi_connect_state(snapshot_attempt)

        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", conn_name],
            capture_output=True, text=True, timeout=15,
        )
        proc = subprocess.run(["sudo", "nmcli", *nm_args], capture_output=True, text=True, timeout=60)

        stdout = proc.stdout.strip() if proc.stdout else ""
        stderr = proc.stderr.strip() if proc.stderr else ""
        combo = "\n".join(x for x in (stdout, stderr) if x)
        ok = proc.returncode == 0

        finish = {
            **snapshot_attempt,
            "status": "connected" if ok else "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "nmcli_exit_code": proc.returncode,
        }
        if not ok:
            finish["stderr"] = combo[:4096]

        _write_wifi_connect_state(finish)

        if ok:
            logger.info("Wi-Fi ligado (ssid=%s perfil=%s)", ssid, conn_name)
            return True, ""

        logger.error("Wi-Fi falhou: %s", combo or proc.returncode)
        return False, combo or "Falha ao executar nmcli."

    except subprocess.TimeoutExpired as e:
        combo = str(e)
        logger.error("Timeout ao ligar Wi-Fi: %s", e)
        fail = {
            **snapshot_attempt,
            "status": "timeout",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "stderr": combo[:4096],
        }
        try:
            _write_wifi_connect_state(fail)
        except Exception:
            pass
        return False, combo

    except Exception as e:
        combo = str(e)
        logger.error("Erro inesperado ao ligar Wi-Fi: %s", e)
        fail = {
            **snapshot_attempt,
            "status": "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "stderr": combo[:4096],
        }
        try:
            _write_wifi_connect_state(fail)
        except Exception:
            pass
        return False, combo


def get_wifi_info() -> dict:
    """Retorna informações da conexão Wi-Fi atual."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,DEVICE", "device", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                return {
                    "connected": True,
                    "ssid": parts[1],
                    "signal": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
                    "device": parts[3] if len(parts) > 3 else "wlan0",
                }
    except Exception as e:
        logger.warning("get_wifi_info error: %s", e)
    return {"connected": False, "ssid": "", "signal": 0, "device": "wlan0"}


def disconnect_wifi() -> tuple[bool, str]:
    """Desliga `wlan0` só com nmcli como utilizador do serviço (sem sudo)."""
    try:
        subprocess.run(
            ["nmcli", "device", "disconnect", AP_IFACE],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        err = ((e.stderr or "") + (e.stdout or "")).strip()
        return False, err or str(e)


def forget_saved_wifi_networks() -> tuple[bool, str]:
    """Apaga filas/cache Wi‑Fi em disco, perfis NM ``av-signage-wifi-*`` e desliga STA em `wlan0`
    se **não** estiver em modo AP (para não derrubar o hotspot)."""
    errs: List[str] = []
    with _wifi_assoc_lock:
        for path in (WIFI_BOOT_ASSOC_FILE, WIFI_PENDING_CONNECT_FILE, WIFI_SCAN_CACHE_FILE):
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                errs.append(f"{path.name}: {e}")

        try:
            show = subprocess.run(
                ["nmcli", "-g", "NAME", "connection", "show"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if show.returncode != 0 or not (show.stdout or "").strip():
                show = subprocess.run(
                    ["nmcli", "-t", "-f", "NAME", "connection", "show"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            names = [ln.strip() for ln in show.stdout.splitlines() if ln.strip()]
            for name in names:
                if not name.startswith(NM_WIFI_CONN_PREFIX):
                    continue
                dr = subprocess.run(
                    ["sudo", "nmcli", "connection", "delete", name],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if dr.returncode != 0:
                    combo = ((dr.stderr or "") + (dr.stdout or "")).strip()
                    errs.append(f"delete {name}: {combo or dr.returncode}")
        except Exception as e:
            errs.append(str(e))

        if not is_hotspot_active():
            try:
                subprocess.run(
                    ["sudo", "nmcli", "device", "disconnect", AP_IFACE],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception as e:
                errs.append(f"disconnect: {e}")

    if errs:
        logger.warning("forget_saved_wifi_networks: %s", errs)
        return False, "; ".join(errs[:5])
    logger.info("Esquecer Wi‑Fi: filas/perfil STA removidos.")
    return True, ""
