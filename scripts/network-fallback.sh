#!/bin/bash
# AV Signage Player — Fallback de rede
# Aguarda DHCP por 15s; se não obtiver IP, configura 192.168.50.10/24 como fallback.

FALLBACK_IP="192.168.50.10"
FALLBACK_MASK="24"
CONN_NAME="av-signage-fallback"
STATE_FILE="/opt/av-signage/config/state.json"
LOG_TAG="av-signage-fallback"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    logger -t "$LOG_TAG" "$1"
}

get_ip() {
    hostname -I 2>/dev/null | awk '{print $1}'
}

update_state() {
    local mode="$1"
    if [ -f "$STATE_FILE" ]; then
        python3 -c "
import json, sys
try:
    data = json.load(open('$STATE_FILE'))
except:
    data = {}
data['network_mode'] = '$mode'
json.dump(data, open('$STATE_FILE','w'), indent=2)
" 2>/dev/null
    else
        mkdir -p /opt/av-signage/config
        echo '{"manual_override":false,"network_mode":"'"$mode"'","last_playlist":"","active_schedule_id":""}' > "$STATE_FILE"
    fi
}

log "Verificando conectividade DHCP..."

# Aguardar até 15s para obter IP via DHCP
TIMEOUT=15
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    IP=$(get_ip)
    if [ -n "$IP" ] && [ "$IP" != "127.0.0.1" ]; then
        log "IP obtido via DHCP: $IP"
        update_state "dhcp"
        exit 0
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

log "DHCP falhou. Ativando fallback IP: $FALLBACK_IP/$FALLBACK_MASK"

# Detectar interface ethernet
ETH_IFACE=$(nmcli -t -f DEVICE,TYPE device | grep ":ethernet" | head -1 | cut -d: -f1)
if [ -z "$ETH_IFACE" ]; then
    ETH_IFACE="eth0"
fi

# Remover conexão fallback anterior se existir
nmcli connection delete "$CONN_NAME" 2>/dev/null

# Criar conexão IP fixo de fallback
nmcli connection add \
    type ethernet \
    con-name "$CONN_NAME" \
    ifname "$ETH_IFACE" \
    ipv4.method manual \
    ipv4.addresses "${FALLBACK_IP}/${FALLBACK_MASK}" \
    ipv4.gateway "" \
    ipv4.dns "" \
    -- 2>&1

nmcli connection up "$CONN_NAME" 2>&1

log "Fallback configurado: $FALLBACK_IP/$FALLBACK_MASK na interface $ETH_IFACE"
update_state "fallback"

exit 0
