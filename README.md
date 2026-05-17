# AV Signage Player

**Versão atual: v0.2.1**

Player de digital signage **autônomo** (conteúdo local, sem streaming contínuo), desenvolvido para substituir o sistema X2O descontinuado. O **dispositivo oficial alvo** é o **Android em hardware Scala com SoC RK3399**; a implementação em **Raspberry Pi 5 + Linux** é o **protótipo de referência** para validar fluxo, API e UX antes da migração da stack para Android.

![Release](https://img.shields.io/badge/release-v0.2.1-blue)
![Status](https://img.shields.io/badge/Fase%201-Pi%20protótipo%20validado-green)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![Plataforma](https://img.shields.io/badge/Raspberry%20Pi-5%20(linux)-c51a4a)
![Roadmap](https://img.shields.io/badge/Alvo%20oficial-Android%20RK3399%20(Scala)-3DDC84)

Documentação completa de arquitetura e plano Android: **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

---

## Escopo da v0.2.1 (protótipo Linux / Pi)

- **Interface web** autenticada (Jinja2 + CSS único), acesso por IP ou **mDNS** (`signage-xxx.local:8080`).
- **Mídias**: upload, listagem, remoção; suporte no player conforme codecs do mpv.
- **Playlists** com loop, **stinger** (vinheta), itens ordenados.
- **Agenda** (agendamentos por horário) com **playlist padrão** quando não há janela ativa; runner no backend.
- **Player** mpv fullscreen: play/stop/restart/loop, faixa anterior/próxima, **rotação HDMI** e **overlay de relógio** (Lua), standby com tela de status / fundo personalizado.
- **Rede**: DHCP ou IP fixo; **Wi‑Fi** (NetworkManager): varredura, ligação, **modo AP** (hotspot) para configuração sem Ethernet, fila **pós‑reboot**, esquecer redes salvas.
- **Tela HDMI**: status com IP, mDNS, QR e pareamento; **tela dedicada modo AP** alinhada ao estilo da tela de status.
- **Sistema**: reboot, reset (mantém mídias), factory reset, **data/hora** (NTP / painel quando aplicável).
- **Identidade**: `identity.json` (gitignored); no repositório existe [`config/identity.example.json`](config/identity.example.json) com versão manifestada.

Servidor central (Fase 2) ainda não faz parte deste repositório; o player expõe **código de pareamento** e contratos pensados para evolução.

---

## Tela de status HDMI (resumo)

Quando não há vídeo ou em standby, o monitor pode exibir layout com IP, hostname **.local**, URL de acesso, **QR** (mDNS) e **código de pareamento** — ver geração em `app/services/display_service.py`.

---

## Stack técnica (protótipo Raspberry Pi 5)

| Camada | Tecnologia |
|--------|------------|
| SO | Raspberry Pi OS Lite 64-bit |
| Runtime | Python 3.11+ |
| Web / API | FastAPI + Jinja2 |
| Modelos / config | Pydantic + JSON em `/opt/av-signage/config/` |
| Vídeo | mpv (+ script Lua overlay de relógio) |
| Telas HDMI | Pillow (PNG gerados) + mpv |
| Rede | NetworkManager / `nmcli` |
| Descoberta | Avahi (mDNS) |
| Serviço | systemd (`av-signage-player.service`) |

---

## Estrutura do projeto (resumo)

```
signage-player/
├── app/
│   ├── main.py                 # FastAPI, rotas web e API
│   ├── models.py               # Pydantic
│   └── services/
│       ├── auth_service.py
│       ├── config_service.py
│       ├── device_service.py
│       ├── display_service.py  # Status / AP / BG em PNG
│       ├── media_service.py
│       ├── network_service.py   # IP fixo, Wi‑Fi, hotspot AP
│       ├── player_service.py    # mpv, playlists, standby
│       ├── playlist_service.py
│       ├── reset_service.py
│       └── schedule_service.py
├── web/
│   ├── templates/              # login, dashboard, media, playlists, schedule, network, settings
│   └── static/
├── config/
│   ├── defaults.json
│   └── identity.example.json
├── scripts/
│   ├── install.sh
│   └── clock_overlay.lua       # overlay mpv
├── docs/
│   └── ARCHITECTURE.md         # Arquitetura, estado atual, plano Android RK3399/Scala
├── systemd/
└── requirements.txt
```

---

## Instalação (Raspberry Pi — protótipo)

### Requisitos

- Raspberry Pi 5, Raspberry Pi OS Lite 64-bit
- Python 3.11+, mpv, Avahi, NetworkManager, fontes DejaVu

### Automática

```bash
git clone https://github.com/caiomaggiore/AV-Signage-Player.git /opt/av-signage
cd /opt/av-signage
sudo bash scripts/install.sh
```

### Primeira identidade (opcional)

Se necessário criar `config/identity.json` a partir do exemplo:

```bash
cp /opt/av-signage/config/identity.example.json /opt/av-signage/config/identity.json
# Ajustar software_version se o equipamento já tiver device_id gerado
```

---

## Acesso

- `http://<hostname>.local:8080` (ex.: `signage-maggiore.local`)
- `http://<IP>:8080`

Primeiro acesso: definição de senha de administração.

---

## API (visão geral)

Todas as rotas que alteram estado ou leem dados sensíveis exigem **sessão autenticada** (cookie), salvo rota pública explícita (ex.: `/health`).

| Área | Exemplos de rotas |
|------|-------------------|
| Status / dispositivo | `GET /api/status`, `GET /api/device`, `GET /api/network` |
| Rede | `POST /api/network/apply`, `POST /api/network/wifi/scan`, `POST /api/network/wifi/connect`, `POST /api/network/wifi/forget` |
| Mídia | `GET/POST /api/media`, `POST /api/media/upload`, `DELETE /api/media/{filename}` |
| Player | `POST /api/player/play`, `stop`, `restart`, `loop`, `next`, `previous`, `rotation`, `clock-position`, `standby`, `resume-schedule` |
| Playlists | `GET/POST /api/playlists`, `PUT/DELETE /api/playlists/{id}`, `POST .../play` |
| Agendas | `GET/POST /api/schedules`, `PUT/DELETE /api/schedules/{id}`, `POST /api/schedules/fallback` |
| Sistema | `POST /api/system/reboot`, `reset`, `factory`; `GET/POST /api/system/time` |
| Saúde | `GET /health` |

Lista completa e contratos: código em `app/main.py`.

---

## Desenvolvimento e deploy no Pi

```bash
# No PC: commit e push
git push origin main

# No Raspberry
cd /opt/av-signage && git pull && sudo systemctl restart av-signage-player
```

Logs: `journalctl -u av-signage-player -f`

---

## Fases do produto

| Fase | Status | Descrição |
|------|--------|-----------|
| **Fase 1a** | **Em produção no Pi (v0.2.1)** | Player autônomo: web, mídia, playlists, agenda, rede, AP, HDMI |
| **Fase 1b** | Planejada | **Port para Android RK3399 (Scala)** — ver arquitetura |
| **Fase 2** | Planejada | Servidor central, pareamento, biblioteca e comandos remotos |
| **Fase 3** | Planejada | Empacotamento em escala, imagens OTA, hardening |

---

## Roadmap de plataforma

| Plataforma | Papel |
|------------|--------|
| **Raspberry Pi 5 + Linux** | Protótipo validado; referência de comportamento e API |
| **Android + RK3399 (Scala)** | **Alvo oficial** de hardware e SO para produção |

Detalhe técnico do plano Android: **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** (secção «Plano de arquitetura Android»).

---

## Licença

Projeto interno — Maggiore. Todos os direitos reservados.
