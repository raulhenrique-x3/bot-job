# bot-job

Bot que monitora vagas de emprego em portais brasileiros, aplica filtros, faz
análise de compatibilidade com seu currículo usando IA (CLI `agy`) e envia as
melhores vagas para o Telegram com o currículo recomendado anexado.

**Sem candidatura automática** — o bot só avisa; a decisão é sua.

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Como funciona

```
┌────────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  Scrapers  │ → │  Filtros  │ → │ DB (dedupe)│ → │  Análise │ → │ Telegram │
│ 5 portais  │   │ regra/idade│  │  SQLite   │   │   IA     │   │ + PDF    │
└────────────┘   └───────────┘   └──────────┘   └──────────┘   └──────────┘
```

1. **Coleta** — 5 scrapers rodam em sequência:
   - **Programathor** — listagem rica (HTML)
   - **GeekHunter** — listagem + página de detalhe pública
   - **LinkedIn** — requer sessão salva (ver [Login](#login-sessões))
   - **QueroVagas Tech** — API JSON pública
   - **Nerdin** — listagem HTML
2. **Filtros** — idade da vaga, palavras-chave bloqueadas, empresas bloqueadas,
   níveis (estágio/trainee), dedupe por id + hash de título/empresa.
3. **Persistência** — SQLite (`data/jobs.db`) com histórico e status:
   `new → analyzed → notified | skipped_low_match | analysis_failed`.
4. **Análise IA** — a vaga é comparada com seus currículos (PDFs em `resumes/`),
   retornando: match 0–100, skills casadas, gaps, razões e qual currículo usar.
5. **Notificação** — mensagem formatada + PDF do currículo recomendado no Telegram.

## Estrutura

```
bot/
├── main.py            # orquestração: coleta → filtra → analisa → notifica
├── config.py          # logging + helpers de env
├── generate_profile.py# gera perfil consolidado a partir dos currículos
├── login.py           # login interativo (Playwright) para sessões
├── profile.py         # ResumeLibrary (le PDFs, extrai texto)
├── scrapers/
│   ├── base.py        # JobSource (classe abstrata)
│   ├── browser.py     # helpers Playwright (goto, UA, sleep)
│   ├── programathor.py
│   ├── geekhunter.py
│   ├── linkedin.py
│   ├── querovagas.py
│   └── nerdin.py
├── gemini/            # análise IA (via CLI agy, sem API key)
│   ├── analysis.py    # JobAnalyzer + chamada subprocess agy
│   └── prompts.py     # prompts de análise
├── filters/filters.py # content_hash, is_too_old, passes_pre_filters
├── database/db.py     # SQLite (jobs, dedupe, análise, status)
└── telegram/
    ├── client.py      # Bot API client (urllib puro, multipart p/ PDF)
    └── format.py      # formatação da mensagem HTML
```

## Portais

| Portal | Tipo | Requer login |
|---|---|---|
| Programathor | HTML | sim (opcional) |
| GeekHunter | HTML | não |
| LinkedIn | HTML | sim (obrigatório) |
| QueroVagas | API JSON | não |
| Nerdin | HTML | não |

## Instalação

```bash
git clone https://github.com/raulhenrique-x3/bot-job.git
cd bot-job
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env   # preencher TELEGRAM_*
```

IA: usa o binário `agy` (`~/.local/bin/agy`). Sem API key no código.
Modelo configurável via `AGY_MODEL`.

## Login (sessões)

```bash
.venv/bin/python -m bot.login linkedin      # salva state/auth_linkedin.json
.venv/bin/python -m bot.login programathor  # salva state/auth.json
```

Sessions expiram; re-loge quando aparecer "session lost" no log.

## Perfil consolidado (uma vez)

```bash
.venv/bin/python -m bot.generate_profile   # lê resumes/*.pdf → state/profile.md
```

Revisar `state/profile.md` (a IA gera; revise por inventário).

## Execução

```bash
.venv/bin/python -m bot.main   # um ciclo: coleta → filtra → analisa → notifica
```

Agendamento via systemd timer (ver `deploy/`) ou cron:

```
*/30 * * * * cd ~/projetos/bot-job && .venv/bin/python -m bot.main >> logs/cron.log 2>&1
```

## Docker

```bash
docker compose up -d
```

O container roda como scheduler: executa um ciclo e dorme
`SCRAPE_INTERVAL_MINUTES` minutos.

Veja `docs/` para detalhes de arquitetura e configuração.

## Currículos

Coloque seus PDFs em `resumes/` (não versionado). O nome do arquivo (sem
extensão) vira a `resume_key` que a IA escolhe. Ex: `resumes/fullstack.pdf`.
Bot espera ter um ou mais currículos e escolhe o melhor fit por vaga.

## Detalhes técnicos

- Max 8 análises por ciclo; excedente fica `new` para o ciclo seguinte.
- 1 chamada `agy` por vaga nova (~22k tokens in, JSON schema estrito).
- Chromium headless sob demanda; sem processo residente.
- Dedupe por `id` do portal + hash de `título|empresa` (SHA-1).

## License

MIT
