# Instalação & Execução

## Requisitos

- Python 3.12+
- Chromium via Playwright (scraping e preenchimento de formulários sem API)
- CLI `agy` disponível no `PATH` (configurada por padrão para `gemini-4-neon-medium`, effort `medium`)

## 1. Preparar ambiente

```bash
git clone https://github.com/raulhenrique-x3/bot-job.git
cd bot-job
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 2. Configurar `.env`

```bash
cp .env.example .env
```

Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.
Por segurança, `APPLICATION_DRY_RUN=true` vem habilitado no `.env.example` para que você valide análises, currículos ATS gerados e preenchimentos antes de ativar envios reais (`APPLICATION_DRY_RUN=false`).

## 3. Currículos originais e perfil consolidado

Coloque seus currículos em PDF na pasta `resumes/` (não versionada). Ex.:

```
resumes/
├── curriculo_raul_fullstack.pdf
├── curriculo_raul_front_end.pdf
├── curriculo_raul_ai_ml.pdf
├── resume_raul_fullstack.pdf
├── resume_raul_front_end.pdf
└── resume_raul_ai_ml.pdf
```

Gere o perfil consolidado (`state/profile.md` e `state/profile.json`):

```bash
.venv/bin/python -m bot.generate_profile
```

Revise `state/profile.json` e `state/profile.md`. Você pode preencher manualmente em `state/profile.json` campos opcionais como `work_authorization`, `salary_expectation` e `availability` para aumentar o índice de respostas verificadas em formulários.

## 4. Sessões autenticadas (opcional por portal)

```bash
.venv/bin/python -m bot.login linkedin
.venv/bin/python -m bot.login programathor
.venv/bin/python -m bot.login geekhunter
.venv/bin/python -m bot.login gupy
```

As sessões são salvas em `state/auth_*.json` com permissão restrita `0600` (e ignoradas pelo `.gitignore`).

## 5. Executar

### Ciclo manual (recomendado primeiro em `APPLICATION_DRY_RUN=true`)

```bash
.venv/bin/python -m bot.main
```

### Testes automatizados

```bash
.venv/bin/pip install pytest
.venv/bin/pytest -v
```

### Cron / Systemd / Docker

```bash
# Docker (roda em loop a cada SCRAPE_INTERVAL_MINUTES)
docker compose up -d
docker compose logs -f
```

## Segurança

- `.env`, `state/`, `data/` e `resumes/` estão no `.gitignore` e nunca devem ser versionados.
- Logs estruturados mascaram automaticamente tokens do Telegram, headers `Bearer` e credenciais inline.
- O bot nunca tenta contornar CAPTCHA, MFA ou proteções anti-bot; nesses casos gera `pending_human_action` e notifica no Telegram.
