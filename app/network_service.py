from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

HOSTNAME_PREFIX = "signage"
HOSTNAME_MAX_LEN = 63


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
    """Valida se o gateway é válido e está na mesma sub-rede que o IP."""
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
    """Valida configuração completa de IP estático."""
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
    # Verificar que IP não é endereço de rede nem broadcast
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
    """Converte texto para slug válido para hostname (sem acentos, só letras/números/hífen)."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


def build_hostname(device_name: str) -> str:
    """Gera hostname técnico a partir do nome do dispositivo."""
    slug = _slugify(device_name)
    if not slug:
        slug = "player"
    hostname = f"{HOSTNAME_PREFIX}-{slug}"
    return hostname[:HOSTNAME_MAX_LEN]


def validate_hostname(hostname: str) -> tuple[bool, str]:
    """Valida hostname final (com prefixo signage-)."""
    if not hostname.startswith(f"{HOSTNAME_PREFIX}-"):
        return False, f"Hostname deve começar com '{HOSTNAME_PREFIX}-'."
    if "_" in hostname:
        return False, "Hostname não pode conter underscore."
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", hostname):
        return False, "Hostname deve conter apenas letras minúsculas, números e hífen, sem começar ou terminar com hífen."
    return True, ""


def apply_hostname(hostname: str) -> tuple[bool, str]:
    """Aplica novo hostname via hostnamectl e atualiza /etc/hosts."""
    ok, msg = validate_hostname(hostname)
    if not ok:
        return False, msg
    try:
        subprocess.run(
            ["sudo", "hostnamectl", "set-hostname", hostname],
            timeout=10, check=True, capture_output=True,
        )
        # Atualizar /etc/hosts via sudo tee para evitar warnings
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
# NetworkManager / nmcli
# ---------------------------------------------------------------------------

NM_CONN_NAME = "av-signage-eth"


def _get_eth_interface() -> str:
    """Retorna a primeira interface ethernet ativa."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "ethernet" and parts[2] == "connected":
                return parts[0]
        # Fallback: primeira interface ethernet
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "ethernet":
                return parts[0]
    except Exception as e:
        logger.warning("Não foi possível detectar interface: %s", e)
    return "eth0"


def apply_dhcp(interface: Optional[str] = None) -> tuple[bool, str]:
    """Configura interface para DHCP via nmcli."""
    iface = interface or _get_eth_interface()
    try:
        # Remover conexão gerenciada se existir
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", NM_CONN_NAME],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "nmcli", "connection", "add",
             "type", "ethernet",
             "con-name", NM_CONN_NAME,
             "ifname", iface,
             "ipv4.method", "auto"],
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
    """Configura IP estático via nmcli."""
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
            "type", "ethernet",
            "con-name", NM_CONN_NAME,
            "ifname", iface,
            "ipv4.method", "manual",
            "ipv4.addresses", f"{ip}/{prefix}",
            "ipv4.gateway", gateway,
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
    """Retorna informações da conexão de rede atual via nmcli."""
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
