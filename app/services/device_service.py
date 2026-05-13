from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_HARDWARE_PROFILE_MAP = {
    "raspberry pi 5": "raspberry_pi_5",
    "raspberry pi 4": "raspberry_pi_4",
    "raspberry pi 3": "raspberry_pi_3",
    "raspberry pi 2": "raspberry_pi_2",
    "raspberry pi zero": "raspberry_pi_zero",
}


def get_hardware_profile(model: str) -> str:
    lower = model.lower()
    for key, profile in _HARDWARE_PROFILE_MAP.items():
        if key in lower:
            return profile
    return "linux_generic"


def get_serial_number() -> str:
    try:
        content = Path("/proc/cpuinfo").read_text()
        for line in content.splitlines():
            if line.startswith("Serial"):
                return line.split(":")[1].strip()
    except Exception:
        pass
    try:
        return Path("/proc/device-tree/serial-number").read_text().strip("\x00").strip()
    except Exception:
        return ""


def get_os_version() -> str:
    try:
        content = Path("/etc/os-release").read_text()
        for line in content.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "Linux"


def get_mac(interface: str) -> str:
    try:
        return Path(f"/sys/class/net/{interface}/address").read_text().strip()
    except Exception:
        return ""


def get_active_connection_type() -> str:
    """Detecta se a conexão ativa é ethernet ou wifi."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[2] == "connected":
                if parts[1] == "wifi":
                    return "wifi"
                if parts[1] == "ethernet":
                    return "ethernet"
    except Exception:
        pass

    # Fallback: checar se interface tem IP
    for iface, conn_type in [("eth0", "ethernet"), ("wlan0", "wifi")]:
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", iface],
                capture_output=True, text=True, timeout=3,
            )
            if "inet " in result.stdout:
                return conn_type
        except Exception:
            pass
    return "none"


def get_ip() -> str:
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ips = result.stdout.strip().split()
        return ips[0] if ips else ""
    except Exception:
        return ""


def get_device_info() -> dict:
    """Retorna informações completas do hardware."""
    from app.services.config_service import config_service

    identity = config_service.identity
    cfg = config_service.config

    model = identity.hardware_model
    serial = get_serial_number()
    eth_mac = get_mac("eth0")
    wifi_mac = get_mac("wlan0")
    active_type = get_active_connection_type()
    ip = get_ip()

    return {
        "device_id": identity.device_id,
        "display_name": cfg.display_name,
        "device_name": cfg.device_name,
        "hardware_profile": get_hardware_profile(model),
        "platform": "linux",
        "model": model,
        "serial_number": serial,
        "app_version": identity.software_version,
        "os_version": get_os_version(),
        "network": {
            "active_type": active_type,
            "ip": ip,
            "ethernet_mac": eth_mac,
            "wifi_mac": wifi_mac,
        },
    }
