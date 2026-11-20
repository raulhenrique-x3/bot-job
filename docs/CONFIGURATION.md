# Configuração (.env)

Copie `.env.example` para `.env` e ajuste os parâmetros desejados.

## IA (`agy` CLI)

| Variável | Default | Descrição |
|---|---|---|
| `AGY_MODEL` | `gemini-4-neon-medium` | Modelo de IA (Gemini 4 Neon) |
| `AGY_REASONING_EFFORT` | `medium` | Nível de raciocínio (`low\|medium\|high\|xhigh\|max`) |
| `AGY_TIMEOUT_SECONDS` | `300` | Timeout por chamada `agy` |
| `AGY_MAX_RETRIES` | `2` | Retentativas em falhas transitórias da CLI `agy` |

## Telegram

| Variável | Default | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token do `@BotFather` |
| `TELEGRAM_CHAT_ID` | — | Chat/canal de destino |

## Descoberta e Compatibilidade

| Variável | Default | Descrição |
|---|---|---|
| `SCRAPE_INTERVAL_MINUTES` | `30` | Intervalo entre ciclos (Docker / scheduler) |
| `JOB_MAX_AGE_DAYS` | `14` | Descarta vagas mais antigas que N dias |
| `JOB_MATCH_THRESHOLD` | `75` | Compatibilidade mínima (0–100) para avançar |
| `MAX_ANALYSES_PER_RUN` | `8` | Limite de análises de IA por ciclo |
| `DEFAULT_LANGUAGE` | `pt` | Idioma padrão quando detecção automática for inconclusiva |
| `MAX_PAGES_PER_PORTAL` | `2` | Páginas coletadas por portal/termo |

## Automação de Candidaturas

| Variável | Default | Descrição |
|---|---|---|
| `AUTO_APPLY_ENABLED` | `true` | `true`: pipeline completo de candidatura; `false`: modo apenas recomendação |
| `APPLICATION_DRY_RUN` | `true` | Modo simulação: analisa, gera PDFs e preenche formulário **sem enviar** |
| `MAX_APPLICATIONS_PER_RUN` | `5` | Máximo de candidaturas processadas por ciclo |
| `MAX_APPLICATIONS_PER_DAY` | `20` | Teto diário de candidaturas reais enviadas |
| `APPLICATION_INTERVAL_SECONDS` | `30` | Intervalo em segundos entre candidaturas |
| `APPLICATION_MAX_RETRIES` | `3` | Máximo de retentativas para falhas temporárias |
| `APPLICATION_RETRY_BACKOFF_MINUTES` | `30` | Base do backoff exponencial entre retentativas |
| `APPLICATION_TIMEOUT_SECONDS` | `60` | Timeout por interação/envio na plataforma |

## Diretórios e Logs

| Variável | Default | Descrição |
|---|---|---|
| `RESUMES_DIR` | `resumes` | Pasta com currículos originais (PDF) |
| `APPLICATIONS_DIR` | `data/applications` | Diretório dos documentos gerados por vaga |
| `DATABASE_PATH` | `data/jobs.db` | Caminho do banco SQLite |
| `PROFILE_JSON_PATH` | `state/profile.json` | Perfil estruturado verificado |
| `PROFILE_MD_PATH` | `state/profile.md` | Perfil consolidado em Markdown |
| `LOCK_FILE` | `state/jobbot.lock` | Lock exclusivo de concorrência |
| `LOG_LEVEL` | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `json` | Formato de log (`json` estruturado ou `text`) |

## Scrapers e Pré-filtros

| Variável | Default | Descrição |
|---|---|---|
| `PROGRAMATHOR_ENABLED` | `true` | Habilita scraper Programathor |
| `GEEKHUNTER_ENABLED` | `true` | Habilita scraper GeekHunter |
| `LINKEDIN_ENABLED` | `true` | Habilita scraper LinkedIn |
| `QUEROVAGAS_ENABLED` | `true` | Habilita scraper QueroVagas |
| `NERDIN_ENABLED` | `true` | Habilita scraper Nerdin |
| `REMOTIVE_ENABLED` | `true` | Habilita scraper Remotive (API JSON pública) |
| `HIMALAYAS_ENABLED` | `true` | Habilita scraper Himalayas (API JSON pública) |
| `WWR_ENABLED` | `true` | Habilita scraper We Work Remotely (RSS público) |
| `JOBICY_ENABLED` | `true` | Habilita scraper Jobicy (API JSON v2 pública) |
| `JOBICY_GEOS` | `latam,anywhere` | Regiões consultadas na API do Jobicy |
| `WORKINGNOMADS_ENABLED` | `true` | Habilita scraper Working Nomads (API JSON pública) |
| `QUEROVAGAS_WORK_MODE` | `Remote` | Filtro de modalidade QueroVagas |
| `LINKEDIN_USE_PROFILE_QUERIES` | `true` | Gera termos de busca do LinkedIn automaticamente a partir do perfil (`state/profile.json`) |
| `LINKEDIN_EASY_APPLY_ONLY` | `true` | Filtra buscas do LinkedIn apenas para vagas com Easy Apply (`f_AL=true`) |
| `LINKEDIN_REMOTE_ONLY` | `true` | Filtra buscas do LinkedIn apenas para vagas remotas (`f_WT=2`) |
| `LINKEDIN_LOCATION` | `Brasil` | Localização usada nas buscas do LinkedIn |
| `LINKEDIN_SEARCH_TERMS` | `desenvolvedor` | Termos manuais separados por vírgula (usado quando `LINKEDIN_USE_PROFILE_QUERIES=false`) |
| `BLOCKED_TITLE_KEYWORDS` | — | Substrings no título para ignorar |
| `BLOCKED_COMPANIES` | — | Empresas para ignorar |

## Consultas úteis no SQLite

```sql
-- Resumo de vagas por status
SELECT status, COUNT(*) FROM jobs GROUP BY status;

-- Candidaturas enviadas e pendentes de ação humana
SELECT job_id, company, title, platform, status, match_score, confirmation_id, applied_at
FROM applications
ORDER BY updated_at DESC
LIMIT 20;

-- Trilha de auditoria de uma vaga específica
SELECT created_at, status, detail
FROM application_events
WHERE job_id = 'gh:123'
ORDER BY id ASC;
```
