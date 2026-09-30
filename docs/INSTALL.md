# Instalação & Execução

## Requisitos

- Python 3.12+
- Chromium (via Playwright) para scraping
- binário `agy` para análise IA (opcional: sem ele, jobs ficam `new`)

## 1. Clonar e preparar ambiente

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

Campos obrigatórios para notificação:

```
TELEGRAM_BOT_TOKEN=...   # @BotFather
TELEGRAM_CHAT_ID=...     # seu chat ou canal
```

Veja [CONFIGURATION.md](CONFIGURATION.md) para a lista completa de variáveis.

## 3. Sessões (LinkedIn, Programathor)

```bash
.venv/bin/python -m bot.login linkedin
.venv/bin/python -m bot.login programathor
```

Abre navegador real; faça login e dê ENTER no terminal. Sessions salvam em
`state/auth_*.json` (não versionado). Re-login quando expirar.

## 4. Currículos

Coloque os PDFs em `resumes/`:

```
resumes/
├── fullstack.pdf
└── frontend.pdf
```

O `resume_key` = nome do arquivo sem extensão. A IA escolhe o melhor fit por
vaga e o bot envia o PDF junto.

Um run sem currículo não quebra: análise e notificação ficam desativadas.

## 5. Perfis consolidado (opcional)

```bash
.venv/bin/python -m bot.generate_profile
```

Gera `state/profile.md` (revise o conteúdo).

## 6. Rodar

### Manual / cron

```bash
.venv/bin/python -m bot.main
```

Cron (a cada 30 min):

```
*/30 * * * * cd ~/projetos/bot-job && .venv/bin/python -m bot.main >> logs/cron.log 2>&1
```

### systemd timer

```bash
sudo cp deploy/jobbot.service /etc/systemd/system/jobbot@.service
sudo cp deploy/jobbot.timer    /etc/systemd/system/jobbot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now jobbot.timer@seuusuario.timer
```

### Docker

```bash
docker compose up -d
```

O container faz scraping, análise e dorme `SCRAPE_INTERVAL_MINUTES` entre
ciclos. Volumes montam `data/`, `state/`, `resumes/`.

## Logs

- stdout/stderr: formato `%(asctime)s %(levelname)s %(name)s: %(message)s`
- Systemd: `journalctl -u jobbot@seuusuario -f`
- Docker: `docker compose logs -f`

## Segurança

- `.env` contém token do Telegram → **nunca** versionar.
- `state/auth_*.json` contém cookies de sessão → **nunca** versionar.
- `data/jobs.db` e `resumes/` ficam fora do git.
