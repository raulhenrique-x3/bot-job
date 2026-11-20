# Arquitetura

## Visão geral

O `bot-job` executa ciclos curtos, idempotentes e protegidos por lock exclusivo (`state/jobbot.lock`), disparados manualmente, via systemd timer, cron ou Docker:

```
run_lock + recover_interrupted()
   │
   ├─► 1. Scrapers (6 portais) ──► 2. Pré-filtros & Dedupe ──► SQLite (jobs: discovered)
   │
   ├─► 3. JobAnalyzer (agy: gemini-4-neon-medium / effort=medium)
   │      ├─ incompatível ou blocker eliminatório ──► status: rejected
   │      └─ compatível (>= threshold) ──► status: analyzed
   │
   ├─► 4. TailoredResumeGenerator (ATS single-column PDF + validação pypdf/veracidade)
   │      └─ data/applications/{job_id}/resume.md + {Nome_Candidato}_Curriculo.pdf (ou _Resume.pdf) + metadata.json
   │
   ├─► 5. ApplierRegistry (detecção de plataforma → inspeção → carta sob demanda → envio)
   │      ├─ API oficial (Greenhouse / Lever) + PlaywrightFormFiller
   │      ├─ DRY-RUN: preenche e valida sem clicar em enviar ──► documents_ready
   │      ├─ Bloqueio (CAPTCHA/MFA/legal/carta recomendação/dúvida) ──► pending_human_action
   │      ├─ Confirmação explícita da plataforma ──► applied (+ confirmation_id)
   │      └─ Falha temporária ──► application_failed (retentativa com backoff exponencial)
   │
   └─► 6. TelegramClient (resumo HTML + PDF com nome humano + carta opcional + alertas)
```

## Estados no banco SQLite (`data/jobs.db`)

O banco contém três tabelas (`jobs`, `applications` e `application_events`) com migração automática sobre bancos antigos.

Ciclo de vida suportado:
- `discovered` (compatível com legado `new`): vaga coletada e filtrada.
- `analyzed`: compatibilidade avaliada pela IA.
- `rejected`: nota abaixo do mínimo ou requisito eliminatório não atendido.
- `preparing_documents`: gerando currículo personalizado ATS.
- `documents_ready`: documentos validados (também usado como estado final de simulações `dry_run`).
- `applying`: tentativa de envio em andamento.
- `applied`: envio **explicitamente confirmado** pela plataforma (com comprovante/ID quando disponível).
- `pending_human_action`: bloqueio de segurança ou pergunta não verificada (CAPTCHA, MFA, declaração legal, carta de recomendação de terceiros, login expirado ou ausência de confirmação).
- `application_failed`: falha técnica (agendada para retentativa controlada até `APPLICATION_MAX_RETRIES`).
- `expired`: vaga encerrada detectada na página de detalhe ou candidatura.

## Recuperação pós-reinicialização e Idempotência

- **Trava de concorrência (`bot/config.py:run_lock`)**: `fcntl.flock` exclusivo impede dois ciclos simultâneos.
- **Recuperação (`Database.recover_interrupted`)**:
  - `preparing_documents` interrompido volta para `analyzed` (seguro para retomar).
  - `applying` interrompido vai para `pending_human_action` (evita reenvio duplicado caso o submit já tenha sido processado antes da queda).
- **Idempotência (`Database.has_duplicate_application`)**: bloqueia candidaturas duplicadas tanto pelo `job_id` quanto pelo `content_hash` (`título|empresa`) entre portais diferentes.

## Geração de Documentos (`bot/documents/`)

Cada candidatura recebe diretório isolado em `data/applications/{job_id_sanitizado}/`:
- `resume.md` e `{Nome_Candidato}_Curriculo.pdf` (PT) ou `{Nome_Candidato}_Resume.pdf` (EN) — nome humano compatível com recrutadores/ATS
- `cover_letter.md` e `{Nome_Candidato}_Carta_Apresentacao.pdf` (PT) ou `{Nome_Candidato}_Cover_Letter.pdf` (EN) *(apenas quando a plataforma solicita carta de apresentação)*
- `metadata.json` *(metadados, validações, plataforma, status, comprovante, campos preenchidos)*

Regras aplicadas:
- Idioma adaptado automaticamente ao idioma da vaga (`pt` ou `en`).
- PDF de coluna única via ReportLab (`DejaVuSans` com fallback seguro para `Helvetica`), sem tabelas, colunas múltiplas, ícones ou gráficos.
- Validador pós-geração verifica legibilidade com `pypdf`, contagem de páginas, nome do candidato e impede datas/anos ausentes dos currículos originais.

## Automação de Candidaturas (`bot/appliers/`)

Arquitetura baseada em `PlatformApplier` + `ApplierRegistry` (basta implementar uma subclasse e registrar com `registry.register(...)`):

- **Greenhouse** (`greenhouse.py`): consulta a API pública Job Board (`?questions=true`) para inspecionar campos, cartas e bloqueios, detecta vagas encerradas e preenche formulários (incluindo React-Select `combobox`) via Playwright.
- **Lever** (`lever.py`): consulta Lever Postings API (`/v0/postings/...`) e preenche `/apply`.
- **Ashby** (`ashby.py`), **InHire** (`inhire.py`), **Gupy** (`gupy.py`), **LinkedIn** (`linkedin.py`, com suporte ao modal SDUI 2026, desempacotamento de `/safety/go/` para ATS externo e trava de identidade da conta), **Programathor / GeekHunter / Nerdin** (`portals.py`), **Email** (`EmailApplier`) e **GenericWebApplier** (`generic.py`).
- **Estimativa salarial com pesquisa Glassdoor (`salary.py`)**: quando um formulário solicita pretensão salarial, `SalaryBudgetEstimator` consulta referências públicas do Glassdoor e ajusta o valor com IA ao escopo/senioridade/modalidade (CLT vs PJ, mensal/hora/anual), sempre respeitando os limites `[salary_min, salary_max]` configurados em `state/profile.json`. Nunca inventa salário atual.
- **Sessões unificadas (`scrapers/browser.py`)**: `load_merged_storage_state("state")` mescla automaticamente todos os arquivos `state/auth*.json` (`auth.json`, `auth_linkedin.json`, `auth_gupy.json`, etc.) no contexto Playwright.
- **Guardrails obrigatórios (`base.py` / `form_filler.py`)**: detecção DOM-aware de CAPTCHA ativo (sem falsos positivos em scripts passivos), nunca tenta resolver CAPTCHA, contornar MFA/login ou inventar declarações legais/cartas de recomendação. Exige confirmação explícita de envio na URL ou texto da resposta.

## IA (`bot/gemini/client.py`)

Todas as chamadas usam o binário local `agy` configurado por padrão para `AGY_MODEL=gemini-4-neon-medium` e `AGY_REASONING_EFFORT=medium` (com `--model` e `--effort`), saída estruturada via `--json-schema` e retentativas com backoff.
