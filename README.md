# bot-job

Agente automatizado de descoberta, análise, geração de documentos ATS e candidatura a vagas de emprego com notificações pelo Telegram, utilizando **Gemini 4 Neon (Medium)** via CLI `agy`.

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Como funciona

```
┌────────────┐   ┌───────────┐   ┌──────────┐   ┌────────────┐   ┌────────────┐   ┌──────────┐
│  Scrapers  │ → │  Filtros  │ → │ Análise  │ → │ Documentos │ → │ Candidatura│ → │ Telegram │
│ 6 portais  │   │ + dedupe  │   │ IA (Neon)│   │ ATS (PDF)  │   │ (API/Web)  │   │ + PDFs   │
└────────────┘   └───────────┘   └──────────┘   └────────────┘   └────────────┘   └──────────┘
```

1. **Coleta (6 portais)** — Programathor, GeekHunter, LinkedIn, QueroVagas Tech, DevAgas e Nerdin.
2. **Filtros e deduplicação** — Idade máxima da vaga, palavras-chave/empresas bloqueadas, níveis bloqueados e dedupe por ID + SHA-1 (`título|empresa`).
3. **Análise inteligente com IA (`gemini-4-neon-medium`, effort `medium`)** — Avalia compatibilidade técnica, experiência real, senioridade, stack obrigatória/desejável, requisitos eliminatórios, idioma (`pt`/`en`), modalidade/localização e salário. Vagas incompatíveis ou com blockers eliminatórios são marcadas como `rejected` sem gerar documentos.
4. **Geração de documentos personalizados (ATS)** — Seleciona o currículo-base em `resumes/`, gera `resume.md` e `resume.pdf` no idioma da vaga (coluna única, sem tabelas/colunas/ícones, validado via `pypdf` e checagem de veracidade de datas/fatos) e gera `cover_letter.md` / `cover_letter.pdf` **somente** quando a plataforma solicita carta de apresentação.
5. **Automação de candidaturas extensível (`bot/appliers/`)** — Prioriza APIs oficiais (Greenhouse Job Board API, Lever Postings API) e automação Playwright para formulários web (Ashby, LinkedIn Easy Apply, Gupy, Programathor, GeekHunter, Nerdin, formulários genéricos). Exige confirmação explícita da plataforma antes de marcar como `applied`.
6. **Segurança e intervenção humana** — CAPTCHA, MFA/login, declarações legais/sensíveis, perguntas não verificadas ou pedidos de carta de recomendação de terceiros **nunca** são contornados ou inventados; a vaga vai para `pending_human_action` com notificação no Telegram e link direto + PDFs prontos.
7. **Controle e persistência (`data/jobs.db`)** — Ciclo de vida completo (`discovered → analyzed → rejected | preparing_documents → documents_ready → applying → applied | pending_human_action | application_failed | expired`), idempotência contra candidaturas duplicadas, retentativas controladas, recuperação pós-crash e trava de concorrência (`flock`).
8. **Modo Dry-Run (`APPLICATION_DRY_RUN=true`)** — Executa descoberta, análise, geração/validação dos PDFs e preenchimento do formulário **sem clicar em enviar**.

## Estrutura

```
bot/
├── main.py                  # orquestração com lock, recuperação pós-crash e limites
├── config.py                # Settings tipadas, logs estruturados JSON, redação de segredos, run_lock
├── profile.py               # ResumeLibrary (normalização pypdf) + CandidateProfile
├── generate_profile.py      # gera state/profile.md + state/profile.json
├── login.py                 # login interativo (Playwright) com permissões 0600
├── scrapers/                # 6 scrapers preservados (Programathor, GeekHunter, LinkedIn, QueroVagas, DevAgas, Nerdin)
├── filters/filters.py       # content_hash, is_too_old, passes_pre_filters, detect_language
├── gemini/
│   ├── client.py            # cliente unificado CLI agy (Gemini 4 Neon, Medium, retries)
│   ├── prompts.py           # prompts de análise, currículo ATS, carta e Q&A verificado
│   └── analysis.py          # JobAnalyzer + should_apply
├── documents/
│   ├── storage.py           # data/applications/{job_id}/ (resume.*, cover_letter.*, metadata.json)
│   ├── pdf_renderer.py      # ReportLab coluna única ATS + validador pypdf
│   ├── resume_generator.py  # TailoredResumeGenerator + checagem de veracidade
│   └── cover_letter.py      # CoverLetterGenerator sob demanda
├── appliers/
│   ├── base.py              # contratos + detectores de CAPTCHA, MFA, legal, confirmação
│   ├── detector.py          # detecção de plataforma e resolução da URL de candidatura
│   ├── form_filler.py       # VerifiedFactAnswerer + PlaywrightFormFiller
│   ├── greenhouse.py        # Greenhouse Job Board API + Playwright
│   ├── lever.py             # Lever Postings API + Playwright
│   ├── ashby.py / gupy.py / linkedin.py / portals.py / generic.py
│   └── registry.py          # ApplierRegistry extensível
├── database/db.py           # SQLite (jobs, applications, application_events) + migração automática
└── telegram/
    ├── client.py            # envio de mensagens e múltiplos PDFs (urllib puro)
    └── format.py            # notificações de candidatura, intervenção humana, dry-run e alertas
tests/                       # suíte pytest (unitários + integração ponta a ponta)
```

## Início rápido

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env         # configurar TELEGRAM_* e revisar APPLICATION_DRY_RUN=true
```

Coloque seus currículos originais em `resumes/` (não versionado) e gere o perfil consolidado:

```bash
.venv/bin/python -m bot.generate_profile   # cria state/profile.md e state/profile.json
```

Salve sessões autenticadas quando necessário:

```bash
.venv/bin/python -m bot.login linkedin
.venv/bin/python -m bot.login programathor
```

Execute um ciclo (em `APPLICATION_DRY_RUN=true` por padrão para validação segura):

```bash
.venv/bin/python -m bot.main
```

Para rodar os testes:

```bash
.venv/bin/pytest -v
```

Consulte [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/CONFIGURATION.md](docs/CONFIGURATION.md) e [docs/INSTALL.md](docs/INSTALL.md) para detalhes completos.

## License

MIT
