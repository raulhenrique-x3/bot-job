# Arquitetura

## Visão geral

O bot roda em ciclos ( disparado por systemd timer, cron ou loop no container ).
Cada ciclo é curto e indempotente: `coleta → filtra → dedupe → persiste → analisa (teto 8/ciclo) → notifica`.

```
                ┌───────────────────────────────────────────────┐
                │                  main.run_once()              │
                │                                               │
   Playwright   │   ┌─────────┐  jobs   ┌─────────┐   fresh     │
  ┌───────────┐ │   │ scrapers│ ───────▶ │ filters │ ────────┐   │
  │ Chromium   │ │   │ x5      │  list[]  │ + dedupe│         │   │
  └───────────┘ │   └─────────┘          └─────────┘         ▼   │
                │                                       ┌──────────┐
                │                                       │ Database │
                │                                       │  SQLite  │
                │                                       └────┬─────┘
                │                                            │ pending (≤8)
                │   ┌──────────┐  analysis  ┌──────────┐     ▼
                │   │ Resume   │ ─────────▶ │ Job      │ ── matches
                │   │ Library  │  prompt    │ Analyzer │
                │   └──────────┘  (agy CLI) └──────────┘
                │                                            │
                │   ┌──────────┐   msg+PDF   ┌──────────┐     │
                │   │ Telegram │ ◀─────────  │ format   │ ◀───┘
                │   │ Client   │             └──────────┘
                │   └──────────┘
                └───────────────────────────────────────────────┘
```

## Fluxo de status das vagas

```
     scraped            analyzed & notified
       │                  ▲
       ▼                  │ match ≥ threshold
     new ──────▶ analyzed ─┬─▶ notified
       │                  │
       │ match < th       │ match ≥ th
       └─ skipped_low     │
                         └─ analysis_failed  (agy erro/JSON inválido)
       │
       └─ expired          (detail fetch detectou vencida)
```

## Scrapers

Cada scraper herda `JobSource` (`scrape() → list[dict]`) e devolve vagas no
formato:

```python
{
    "id": str,          # id único do portal (usado no dedupe)
    "source": str,      # nome do scraper
    "url": str,         # link da vaga
    "title": str,
    "company": str,
    "location": str,
    "modality": str,    # "Remoto" | "Híbrido" | "Presencial"
    "seniority": str,
    "salary": str,
    "posted_at": str,   # ISO 8601 (opcional)
    "body": str,        # descrição (opcional, lazy no analysis)
    "expired": bool,    # opcional
}
```

Detalhes (body) são buscados `lazily` apenas na análise das vagas `new`.

### Estratégias por portal

- **Programathor**: parse HTML da listagem (cards com período/senioridade/etc).
  Erro 500 nas páginas de detalhe → listagem é a única fonte.
- **GeekHunter**: listagem HTML + `fetch_detail` na página pública.
- **LinkedIn**: busca com termos; precisa de `storage_state` (login real).
  Fallback de título quando os cards não têm seletor.
- **QueroVagas**: API JSON pública (`urllib`), Chromium nem carrega.
  Mapeia detalhe por URL.
- **Nerdin**: listagem HTML + paginação via querystring.

## Análise IA

- Chama o binário `agy` via `subprocess` (sem HTTP, sem API key no repo).
- JSON schema estrito garante parse confiável. Parser tolera wrapper/fallbacks.
- Prompt constrói a vaga + currículos (texto extraído dos PDFs por `pypdf`,
  cacheado na `ResumeLibrary`), máx ~3500 chars/resumé.
- Fallback de `resume_key` quando a IA inventa key que não existe.

## Notificação

- `bot/telegram/client.py`: urllib puro (sem `requests`), multipart manual para
  PDF (sendDocument) + HTML message (sendMessage).
- `format.py` monta mensagem HTML com emoji por recomendação:
  ✅ apply / 🟡 maybe / ⛔ skip.

## Dedupe

- `content_hash` = SHA-1 de `título normalizado | empresa normalizada`.
- Vaga duplicada por id OU hash → `touch(last_seen_at)` apenas, não re-notifica.
- Table `jobs` PK = id do portal (o que evita re-análise entre portais diferentes).

## Agendamento

- `deploy/jobbot.timer` — systemd, a cada 30min com delay randomizado.
- `docker/entrypoint.sh` — loop `sleep` no container (scheduler embutido).
- CLI manual: `.venv/bin/python -m bot.main`.
