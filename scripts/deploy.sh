#!/bin/bash
# deploy.sh — Envia arquivos atualizados para o Raspberry Pi e reinicia o serviço.
# Uso: bash deploy.sh [IP_DO_PI]
# Exemplo: bash deploy.sh 192.168.15.22

PI_USER="admin"
PI_IP="${1:-192.168.15.22}"
PI_DIR="/opt/av-signage"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Deploy → $PI_USER@$PI_IP:$PI_DIR ==="

scp -r \
  "$LOCAL_DIR/app" \
  "$LOCAL_DIR/web" \
  "$LOCAL_DIR/scripts/clock_overlay.lua" \
  "$PI_USER@$PI_IP:$PI_DIR/"

# Mover clock_overlay.lua para scripts/
ssh "$PI_USER@$PI_IP" "mv $PI_DIR/clock_overlay.lua $PI_DIR/scripts/clock_overlay.lua 2>/dev/null; true"

# Atualizar sudoers se timedatectl ainda não estiver lá
ssh "$PI_USER@$PI_IP" "
  if ! sudo grep -q 'timedatectl set-timezone' /etc/sudoers.d/av-signage 2>/dev/null; then
    echo 'Atualizando sudoers para timedatectl...'
    sudo sed -i 's|/usr/bin/cp /tmp/etc_hosts /etc/hosts|/usr/bin/cp /tmp/etc_hosts /etc/hosts, /usr/bin/timedatectl set-timezone *, /usr/bin/timedatectl set-ntp *, /usr/bin/systemctl restart systemd-timesyncd|' /etc/sudoers.d/av-signage
  fi
"

echo "Reiniciando serviço..."
ssh "$PI_USER@$PI_IP" "sudo systemctl restart av-signage-player"

echo ""
echo "=== Deploy concluído! ==="
echo "   http://$PI_IP:8080"
