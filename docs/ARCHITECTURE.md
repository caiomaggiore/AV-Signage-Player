# AV Signage Player — Arquitetura, estado atual e roadmap

**Versão documentada:** alinhada à release **v0.2.1** do player Linux (protótipo).

---

## 1. Objetivo e estratégia de plataforma

Solução interna de digital signage para substituir o sistema X2O descontinuado. O conteúdo é **reproduzido localmente** no player; a rede transporta **ficheiros e comandos**, não um fluxo de vídeo contínuo (sem NDI / streaming como núcleo da Fase 1).

| Plataforma | Papel |
|------------|--------|
| **Linux + Raspberry Pi 5** | **Protótipo**: validar fluxos, API, UX e regras de negócio com stack Python/FastAPI/mpv. |
| **Android + RK3399 (hardware Scala)** | **Alvo oficial**: produto em produção na mesma linha funcional, com stack nativa ou híbrida (ver §7). |

O que se pretende **preservar** na migração Android: contratos de configuração (JSON), semântica de playlists/agenda, comportamento de standby/telas, e **paridade funcional** de rede (STA + AP de configuração + mDNS onde o SO permitir).

---

## 2. Fases do produto (atualizado)

### Fase 1 — Player autônomo

**No protótipo Linux (v0.2.1) — entregue / operacional**

- Web local autenticada; API local autenticada.
- Mídias: upload, listagem, remoção.
- **Playlists** (itens, loop, stinger).
- **Agendamentos** por horário + **playlist padrão** quando não há janela ativa.
- Player mpv: play/stop/restart/loop/next/prev; **orientação HDMI**; **overlay de relógio** (Lua).
- Standby com imagem de status (IP, hostname, mDNS, QR, pareamento) ou fundo marca.
- Rede: DHCP/IP fixo; **Wi‑Fi** via NetworkManager; **hotspot AP** para configuração; fila de associação pós‑reboot; cache de scans; esquecer redes.
- Reset (mantém mídias), factory reset, reboot.
- Identidade (`identity.json`), pareamento (`pairing.json`) preparado para Fase 2.
- Serviço **systemd**; logs via journal.

** Planejado na Fase 1 (Android)**

- Port ou reimplementação no dispositivo **Scala RK3399** (§7).

### Fase 2 — Servidor central

(Inalterado em intenção; ainda não implementado neste repositório.)

- Dashboard, cadastro de players, pareamento por código, biblioteca central, comandos remotos, grupos, etc.

### Fase 3 — Produção em escala

- Imagem/OS, OTA, empacotamento, hardening — **adaptado ao canal Android oficial** na variante Scala.

---

## 3. Arquitetura lógica (protótipo Linux)

```text
┌─────────────────────────────────────────────────────────┐
│ Raspberry Pi 5 (Linux)                                   │
│  FastAPI (8080) — Jinja2 UI                              │
│  ├─ Auth / sessão                                        │
│  ├─ Config (defaults + user + state + pairing + identity)│
│  ├─ Device / rede / Wi‑Fi / hotspot                      │
│  ├─ Mídia / playlists / agendas                          │
│  ├─ Display (PNG status, AP screen, standby)             │
│  └─ Tarefas: ScheduleRunner, AP monitor, Wi‑Fi boot queue│
│                                                          │
│  mpv ──► HDMI                                           │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Estado vs documento original (limpeza)

Itens **planejados no doc legado mas ainda inexistentes** como módulos com esse nome:

- `mdns_service.py`, `server_client.py`, `screen_service.py` — a lógica corresponde a **trechos em** `network_service`, `device_service`, `display_service`, `config_service`.
- `GET /api/config` genérico, `GET /api/logs` — **não** expostos como tal; configuração é via páginas e rotas específicas; logs via journal.

Itens **já presentes** e que o doc original listava vagamente ou em outra forma:

- **Playlists**, **schedules**, **resume-schedule**, **standby**, **orientação**, **clock-position**.
- **Wi‑Fi**, **hotspot**, **forget**, **health**.

Estrutura real de serviços em `app/services/`:

`auth_service`, `config_service`, `device_service`, `display_service`, `media_service`, `network_service`, `player_service`, `playlist_service`, `reset_service`, `schedule_service`.

Ficheiros em `config/` versionados como exemplo: `defaults.json`; **`identity.example.json`** (manifesto de versão). `identity.json` e outros permanecem gitignored em instalações reais.

---

## 5. Estrutura de deploy típico (`/opt/av-signage/`)

Congruente com o repositório, com dados gerados em runtime:

```text
/opt/av-signage/
├── app/
├── web/
├── config/          # user_config.json, identity.json, pairing.json, ...
├── media/
├── venv/
├── scripts/
└── systemd/
```

---

## 6. JSON de identidade e pareamento

`identity.example.json` (repositório) espelha campos típicos; em runtime `software_version` pode ser atualizado pelo processo de release/upgrade.

`pairing.json` mantém código e estado para Fase 2.

---

## 7. Plano de arquitetura Android (RK3399 / Scala)

Objetivo: **mesmo produto**, **mesmo papel** no campo (signage autónomo), com camada de sistema adequada ao Android embutido fornecido ou derivado pela **Scala** no SoC **RK3399**.

### 7.1 Princípios

1. **Domínio compartilhado**: playlists, agendas, paths de media e regras de prioridade (agenda vs manual vs fallback) devem ser **documentados** e, idealmente, **testados** de forma isolada para reduzir divergências Pi vs Android.
2. **Player com Surface dedicada**: reprodução em ecrã principal (ExoPlayer / Media3 ou equivalente com suporte a loop, lista e transições onde necessário).
3. **Painel administrativo**: 
   - **Opção A (rápida)**: **WebView em fullscreen** ou painel segundo ecrã apontando para **servidor HTTP embutido** (localhost) — reutilização máxima do HTML atual.
   - **Opção B (nativa)**: UI Kotlin Compose duplicando fluxos — maior custo, melhor integração sistema.
   - Recomendação: **Opção A** na primeira onda Android, com **mini-servidor embutido** (Ktor, NanoHTTPD, ou outro HTTP leve) servindo assets estáticos + API compatível ou adaptada.
4. **Paridade de rede**:
   - Wi‑Fi estação + configurações IP via APIs Android (`ConnectivityManager`, `WifiManager`, ou APIs de sistema se a Scala expuser configurações de rede persistidas).
   - **Soft AP**: `WifiManager.startLocalOnlyHotspot` **não** substitui hotspot com SSID/password legível pelo utilizador na maioria dos OEMs; será necessário **API privada**, **modem tethering**, ou **módulo fornecedor Scala** conforme BSP. Esta é a maior incógnita técnica — marcar spike no início do projeto Android.
5. **mDNS**: `NsdManager` ou biblioteca multimédia bonjour; garantir mesmo sufixo `*.local` ou documentar hostname Android.
6. **Arranque**: `BOOT_COMPLETED` + **Foreground Service** com notificação (requisito Android); mesmo conceito que `systemd`.
7. **Orientação**: `DisplayManager` / `presentation` ou policies por aplicação; alinhar com o que já existe em `orientation`/`rotation`.
8. **Imagem standby**: Compose/View ou mpv/Android **Glide**/ImageView para PNG gerados equivalentes aos da Pillow (ou reusar biblioteca gráfica comum gerando PNG off-thread).

### 7.2 Mapa grosso Pi → Android

| Linux (Pi) | Android (alvo) |
|------------|----------------|
| systemd | Application + BroadcastReceiver + foreground service |
| FastAPI | Ktor/Caddy embutido, ou servidor Python em subprocess (não ideal) |
| mpv | **Media3 / ExoPlayer** |
| Pillow status PNG | Canvas / PDF / Pillow em servidor JVM indireto ou geração em Kotlin |
| nmcli Wi‑Fi / hotspot | WifiManager APIs + tethering/vendor |
| Avahi | NsdManager / DNS-SD |
| JSON config em filesystem | mesmo path em `filesDir`/scoped storage ou ContentProvider interno |

### 7.3 Fases da migração Android

| Sprint | Entrega |
|--------|---------|
| **A — Fundação** | Projeto Gradle, permissões rede/Wi‑Fi/boot, foreground service, ExoPlayer em loop com playlist estática offline. |
| **B — Biblioteca única de regras** | Extrair/reescrever regras de agenda + playlist + fallback em Kotlin (ou KMP); testes unitários. |
| **C — Painel administrativo** | Servidor HTTP local + empacote `web/` (templates podem pré-renderizar onde Jinja não existir) ou primeira versão Compose mínima. |
| **D — Rede cliente** | Ligar STA, formulário IP/DHCP, persistência. |
| **E — Soft AP paridade** | Spike Scala/BSP para SSID/password fixos como no Pi; fallback documentado se só USB/debug. |
| **F — Imagens standby/status** | Paridade visual com modelo atual + QR via biblioteca ZXing. |
| **G — Builds Scala** | Assinatura, OTA opcional, documentação instalação. |

### 7.4 Riscos e dependências

- **Hotspot configurável** pode exigir **acordo com fornecedor Scala** (imagem de sistema ou API).
- **Permissões de escrita em Wi‑Fi** evoluem por versão Android; testar no **API level** da imagem que a Scala entrega.
- Duplicação de UI: se manter Jinja, precisa de **servidor de templates** ou **build step** que gere HTML estático.

---

## 8. API e segurança (resumo)

- Sessão autenticada para rotas sensíveis.
- Alterações de sistema (rede, reboot) apenas através de serviços, não dentro de handlers soltos sem validação.

---

## 9. Referências úteis (desenvolvimento Pi)

```bash
journalctl -u av-signage-player -f
systemctl restart av-signage-player
nmcli connection show
```

---

## 10. Histórico deste documento

- Consolidado em **2026**: alinha documentação legado ao código **v0.2.1** e acrescenta plano oficial **Android RK3399 (Scala)**.
- Para releases futuras: atualizar a secção 2–4 quando o Android substituir o Pi como principal referência runtime.
