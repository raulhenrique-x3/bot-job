# Configuração (.env)

Todas as variáveis são opcionais exceto as de Telegram. Copie `.env.example`.

## Telegram

| Var | Default | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | token do @BotFather |
| `TELEGRAM_CHAT_ID` | — | chat/canal destino |

Sem elas, o bot roda mas notifica não envia (log warning).

## IA (`agy` CLI)

| Var | Default | Descrição |
|---|---|---|
| `AGY_MODEL` | — | override do modelo; usa default do binário |

A IA roda via binário `agy` no PATH. Se não existir, o bot segue sem análise
(jobs ficam `new` para próxima execução).

## Ciclo / Limites

| Var | Default | Descrição |
|---|---|---|
| `SCRAPE_INTERVAL_MINUTES` | `30` | intervalo entre ciclos (Docker loop / timer) |
| `JOB_MAX_AGE_DAYS` | `14` | descarta vaga mais velha que isso |
| `JOB_MATCH_THRESHOLD` | `75` | match % mínimo para notificar |
| `MAX_PAGES_PER_PORTAL` | `2` | paginação por portal/termo |

## Scrapers (on/off switch)

| Var | Default | Descrição |
|---|---|---|
| `PROGRAMATHOR_ENABLED` | `true` | liga/desliga portal |
| `GEEKHUNTER_ENABLED` | `true` | liga/desliga portal |
| `LINKEDIN_ENABLED` | `true` | liga/desliga portal |
| `QUEROVAGAS_ENABLED` | `true` | liga/desliga portal |
| `NERDIN_ENABLED` | `true` | liga/desliga portal |
| `QUEROVAGAS_WORK_MODE` | `Remote` | filtro de modalidade |
| `LINKEDIN_KEYWORDS` | `desenvolvedor` | termo único (uso interno) |
| `LINKEDIN_SEARCH_TERMS` | `desenvolvedor` | termos por vírgula: `python node react` |

## Pré-filtros

| Var | Default | Descrição |
|---|---|---|
| `BLOCKED_TITLE_KEYWORDS` | — | substring no título → skip (ex: `sap, mainframe`) |
| `BLOCKED_COMPANIES` | — | substring na empresa → skip |

Filtros fixos no código: estágio, estagiário, intern, trainee.

## Referência rápida (.env.example)

```ini
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
AGY_MODEL=
SCRAPE_INTERVAL_MINUTES=30
JOB_MAX_AGE_DAYS=14
JOB_MATCH_THRESHOLD=75
MAX_PAGES_PER_PORTAL=2
LINKEDIN_ENABLED=true
PROGRAMATHOR_ENABLED=true
GEEKHUNTER_ENABLED=true
QUEROVAGAS_ENABLED=true
QUEROVAGAS_WORK_MODE=Remote
NERDIN_ENABLED=true
LINKEDIN_SEARCH_TERMS=desenvolvedor python node
BLOCKED_TITLE_KEYWORDS=
BLOCKED_COMPANIES=
```

## Jobs DB

Local: `data/jobs.db` (SQLite).

Colunas relevantes: `status`, `match_score`, `recommendation`,
`resume_key`, `notified_at`, `first_seen_at` / `last_seen_at`.

Consultas úteis:

```sql
-- estatísticas por status
SELECT status, COUNT(*) FROM jobs GROUP BY status;

-- melhores vagas não notificadas
SELECT title, company, match_score
FROM jobs WHERE status='analyzed' ORDER BY match_score DESC LIMIT 20;
```
