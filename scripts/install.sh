#!/bin/bash
set -e

# ── AV Signage Player — install.sh ──────────────────────────────────────────
# Instala e configura o AV Signage Player no Raspberry Pi OS Lite 64-bit.
# Deve ser executado como usuário admin com sudo disponível.
# Uso: sudo bash install.sh
# ────────────────────────────────────────────────────────────────────────────

INSTALL_DIR="/opt/av-signage"
SERVICE_NAME="av-signage-player"
FALLBACK_SERVICE="av-signage-fallback"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
FALLBACK_SERVICE_FILE="/etc/systemd/system/${FALLBACK_SERVICE}.service"
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
echo "  AV Signage Player v0.2 — Instalação"
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
    libzbar0 \
    curl git tree

# ── 2. Estrutura de diretórios ──────────────────────────────────────────────
log "Criando estrutura de diretórios em $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"/{app/services,web/templates,web/static,config,media/{local,server,cache,downloading,factory,backup},logs,scripts,systemd}
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

# ── 6. Script de fallback de rede ──────────────────────────────────────────
log "Configurando script de fallback de rede..."
chmod +x "$INSTALL_DIR/scripts/network-fallback.sh"

# ── 7. Serviços systemd ─────────────────────────────────────────────────────
log "Instalando serviços systemd..."

# Serviço principal
cp "$INSTALL_DIR/systemd/${SERVICE_NAME}.service" "$SERVICE_FILE"
sed -i "s/User=admin/User=${CURRENT_USER}/g" "$SERVICE_FILE"
sed -i "s/Group=admin/Group=${CURRENT_USER}/g" "$SERVICE_FILE"

# Serviço de fallback de rede
cp "$INSTALL_DIR/systemd/${FALLBACK_SERVICE}.service" "$FALLBACK_SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl enable "$FALLBACK_SERVICE"
log "Serviços habilitados para iniciar no boot."

# ── 8. Avahi / mDNS ─────────────────────────────────────────────────────────
log "Habilitando Avahi (mDNS)..."
systemctl enable avahi-daemon
systemctl start avahi-daemon 2>/dev/null || true

# ── 9. NetworkManager ───────────────────────────────────────────────────────
log "Verificando NetworkManager..."
systemctl enable NetworkManager
systemctl start NetworkManager 2>/dev/null || true

# ── 10. Suprimir terminal Linux no HDMI ────────────────────────────────────
log "Desabilitando terminal de login no HDMI (getty@tty1)..."
systemctl disable getty@tty1 2>/dev/null || warn "Não foi possível desabilitar getty@tty1."
systemctl stop getty@tty1 2>/dev/null || true

log "Suprimindo mensagens de boot na saída de vídeo..."
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    if ! grep -q "vt.global_cursor_default=0" "$CMDLINE"; then
        sed -i 's/$/ quiet loglevel=0 vt.global_cursor_default=0 logo.nologo/' "$CMDLINE"
        log "Parâmetros de boot atualizados em $CMDLINE."
    fi
else
    warn "Arquivo $CMDLINE não encontrado."
fi

# ── 11. Inicializar configs padrão se ausentes ──────────────────────────────
log "Verificando arquivos de configuração..."
for cfg in playlists.json schedules.json state.json; do
    if [ ! -f "$INSTALL_DIR/config/$cfg" ]; then
        cp "$INSTALL_DIR/config/$cfg" "$INSTALL_DIR/config/$cfg" 2>/dev/null || true
    fi
done

# ── 12. Iniciar serviços ────────────────────────────────────────────────────
log "Iniciando AV Signage Player..."
systemctl start "$FALLBACK_SERVICE" 2>/dev/null || warn "Fallback service não iniciou."
systemctl start "$SERVICE_NAME" || warn "Serviço não iniciou. Verifique: journalctl -u $SERVICE_NAME -f"

echo ""
echo "=================================================="
echo -e "${GREEN}  Instalação v0.2 concluída!${NC}"
echo "=================================================="
echo ""
echo "  Acesse: http://$(hostname).local:8080"
echo "  Fallback: http://192.168.50.10:8080 (se sem DHCP)"
echo "  Logs:   journalctl -u $SERVICE_NAME -f"
echo "  Status: systemctl status $SERVICE_NAME"
echo ""
