# AV Signage Player

Player de digital signage local para **Raspberry Pi 5**, desenvolvido para substituir o sistema X2O descontinuado. Funciona de forma autônoma — sem depender de servidor central — e está preparado para futura integração com um servidor de controle centralizado.

![Status](https://img.shields.io/badge/Fase%201-Completa-brightgreen)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-5-c51a4a)

---

## Funcionalidades

- **Interface web local** autenticada, acessível por IP ou por `signage-xxx.local`
- **Upload e gerenciamento de vídeos** (.mp4, .mov, .mkv)
- **Player fullscreen** via mpv com controles de play, stop, loop e restart
- **Tela de status HDMI** exibida automaticamente quando não há vídeo tocando
- **Configuração de rede** — DHCP ou IP fixo, com validação completa
- **Renomear dispositivo** com atualização de hostname e mDNS
- **Reset de configuração** mantendo mídias locais
- **Factory reset** apagando configuração e mídias
- **Reboot** pelo painel web
- **Boot autônomo** via serviço systemd
- **Preparado para pareamento** com servidor central (Fase 2)

---

## Tela de status HDMI

Quando nenhum vídeo está sendo reproduzido, o monitor exibe automaticamente:

```
AV Signage Player

IP: 192.168.1.100          HOSTNAME: signage-recepcao-01.local

        Acesse: http://signage-recepcao-01.local:8080

            CÓDIGO DE PAREAMENTO
                  482-913

        v0.1.0 · signage-recepcao-01
```

---

## Stack técnica

| Componente | Tecnologia |
|------------|------------|
| Sistema | Raspberry Pi OS Lite 64-bit |
| Linguagem | Python 3.11+ |
| Web / API | FastAPI + Jinja2 |
| Configuração | JSON + Pydantic |
| Player de vídeo | mpv |
| Tela de status | Pillow + mpv |
| Rede | NetworkManager / nmcli |
| mDNS | Avahi |
| Inicialização | systemd |

---

## Estrutura do projeto

```
signage-player/
├── app/
│   ├── main.py               # FastAPI — rotas web e API
│   ├── models.py             # Modelos Pydantic
│   ├── config_service.py     # Gerenciamento de configuração
│   ├── auth_service.py       # Autenticação e sessão
│   ├── media_service.py      # Upload e listagem de mídias
│   ├── player_service.py     # Controle do mpv
│   ├── network_service.py    # Validação e aplicação de rede
│   ├── reset_service.py      # Reset e factory reset
│   └── screen_service.py     # Tela de status HDMI
│
├── web/
│   ├── templates/            # login, dashboard, mídias, rede, sistema
│   └── static/               # style.css, app.js
│
├── config/
│   └── defaults.json         # Configurações padrão de fábrica
│
├── systemd/
│   └── av-signage-player.service
│
├── scripts/
│   └── install.sh            # Script de instalação
│
└── requirements.txt
```

---

## Instalação

### Requisitos

- Raspberry Pi 5 com Raspberry Pi OS Lite 64-bit
- Python 3.11+
- mpv, Avahi, NetworkManager

### Instalação automática

```bash
# Clonar o repositório
git clone https://github.com/caiomaggiore/AV-Signage-Player.git /opt/av-signage
cd /opt/av-signage

# Executar o instalador
sudo bash scripts/install.sh
```

### Instalação manual

```bash
# Dependências do sistema
sudo apt install -y python3 python3-venv mpv avahi-daemon network-manager \
    fonts-dejavu-core libopenjp2-7

# Ambiente virtual e dependências Python
python3 -m venv /opt/av-signage/venv
/opt/av-signage/venv/bin/pip install -r requirements.txt

# Serviço systemd
sudo cp systemd/av-signage-player.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable av-signage-player
sudo systemctl start av-signage-player
```

---

## Acesso

Após a instalação, acesse pelo navegador:

```
http://signage-maggiore.local:8080
```

Ou pelo IP:

```
http://192.168.x.x:8080
```

No primeiro acesso, será solicitada a criação de uma senha.

---

## API

Todas as rotas sensíveis exigem autenticação via cookie de sessão.

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/status` | Status completo do dispositivo |
| GET | `/api/media` | Listar mídias locais |
| POST | `/api/media/upload` | Upload de vídeo |
| DELETE | `/api/media/{nome}` | Remover mídia |
| POST | `/api/player/play` | Iniciar reprodução |
| POST | `/api/player/stop` | Parar player |
| POST | `/api/player/restart` | Reiniciar vídeo |
| POST | `/api/player/loop` | Alterar modo loop |
| POST | `/api/network/apply` | Aplicar configuração de rede |
| POST | `/api/system/reboot` | Reiniciar dispositivo |
| POST | `/api/system/reset` | Reset mantendo mídias |
| POST | `/api/system/factory` | Factory reset completo |

---

## Desenvolvimento

### Atualizar o Raspberry após mudanças

```bash
# No PC
git add .
git commit -m "descrição da mudança"
git push

# No Raspberry
ssh admin@signage-maggiore.local
cd /opt/av-signage
git pull
sudo systemctl restart av-signage-player
```

### Comandos úteis no Raspberry

```bash
# Ver logs em tempo real
journalctl -u av-signage-player -f

# Status do serviço
systemctl status av-signage-player

# Reiniciar serviço
sudo systemctl restart av-signage-player

# Ver IP
hostname -I

# Testar mDNS
ping signage-maggiore.local
```

---

## Fases do projeto

| Fase | Status | Descrição |
|------|--------|-----------|
| **Fase 1** | Completa | Player stand alone com interface web local |
| Fase 2 | Planejada | Servidor central com dashboard e controle remoto |
| Fase 3 | Planejada | Pacote .deb, imagem customizada e deploy em escala |

---

## Licença

Projeto interno — Maggiore. Todos os direitos reservados.
