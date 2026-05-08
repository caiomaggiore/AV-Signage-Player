#!/bin/bash
set -e

# ── AV Signage Player — install.sh ──────────────────────────────────────────
# Instala e configura o AV Signage Player no Raspberry Pi OS Lite 64-bit.
# Deve ser executado como usuário admin com sudo disponível.
# Uso: sudo bash install.sh
# ────────────────────────────────────────────────────────────────────────────

INSTALL_DIR="/opt/av-signage"
SERVICE_NAME="av-signage-player"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_FILE="/etc/sudoers.d/av-signage"
LOG_FILE="/tmp/av-signage-install.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[AV]${NC} $1" | tee -a "$LOG_FILE"; }
fail() { echo -e "${RED}[ERRO]${NC} $1" | tee -a "$LOG_FILE"; exit 1; }

echo "=================================================="
echo "  AV Signage Player — Instalação"
echo "=================================================="
echo ""

# Verificar usuário
CURRENT_USER="${SUDO_USER:-$(whoami)}"
[ "$CURRENT_USER" = "root" ] && fail "Execute como usuário admin, não como root diretamente."

# Verificar se é Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    warn "Não detectado como Raspberry Pi. Continuando mesmo assim..."
fi

# ── 1. Dependências do sistema ──────────────────────────────────────────────
log "Atualizando lista de pacotes..."
apt-get update -qq

log "Instalando dependências do sistema..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    mpv ffmpeg \
    avahi-daemon \
    network-manager \
    libopenjp2-7 libtiff6 libfreetype6 \
    fonts-dejavu-core \
    curl git tree

# ── 2. Estrutura de diretórios ──────────────────────────────────────────────
log "Criando estrutura de diretórios em $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"/{app,web/templates,web/static,config,media/local,media/server,media/cache,media/downloading,logs,scripts,systemd}
chown -R "$CURRENT_USER:$CURRENT_USER" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/config"

# ── 3. Python venv ──────────────────────────────────────────────────────────
log "Criando ambiente virtual Python..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    sudo -u "$CURRENT_USER" python3 -m venv "$INSTALL_DIR/venv"
fi

log "Instalando dependências Python..."
sudo -u "$CURRENT_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$CURRENT_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 4. Permissões sudoers ───────────────────────────────────────────────────
log "Configurando permissões sudo..."
cat > "$SUDOERS_FILE" << EOF
${CURRENT_USER} ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/bin/hostnamectl, /usr/bin/nmcli, /usr/bin/tee /etc/hosts, /usr/bin/cp /tmp/etc_hosts /etc/hosts
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" || fail "Arquivo sudoers inválido!"

# ── 5. Grupos de acesso (vídeo/DRM) ─────────────────────────────────────────
log "Adicionando $CURRENT_USER aos grupos video e render..."
usermod -aG video,render "$CURRENT_USER" 2>/dev/null || warn "Não foi possível adicionar aos grupos."

# ── 6. Serviço systemd ──────────────────────────────────────────────────────
log "Instalando serviço systemd..."
cp "$INSTALL_DIR/systemd/${SERVICE_NAME}.service" "$SERVICE_FILE"
# Substituir 'admin' pelo usuário atual no arquivo de serviço
sed -i "s/User=admin/User=${CURRENT_USER}/g" "$SERVICE_FILE"
sed -i "s/Group=admin/Group=${CURRENT_USER}/g" "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
log "Serviço $SERVICE_NAME habilitado para iniciar no boot."

# ── 7. Avahi / mDNS ─────────────────────────────────────────────────────────
log "Habilitando Avahi (mDNS)..."
systemctl enable avahi-daemon
systemctl start avahi-daemon 2>/dev/null || true

# ── 8. NetworkManager ───────────────────────────────────────────────────────
log "Verificando NetworkManager..."
systemctl enable NetworkManager
systemctl start NetworkManager 2>/dev/null || true

# ── 9. Iniciar serviço ──────────────────────────────────────────────────────
log "Iniciando AV Signage Player..."
systemctl start "$SERVICE_NAME" || warn "Serviço não iniciou. Verifique: journalctl -u $SERVICE_NAME -f"

echo ""
echo "=================================================="
echo -e "${GREEN}  Instalação concluída!${NC}"
echo "=================================================="
echo ""
echo "  Acesse: http://$(hostname).local:8080"
echo "  Logs:   journalctl -u $SERVICE_NAME -f"
echo "  Status: systemctl status $SERVICE_NAME"
echo ""
