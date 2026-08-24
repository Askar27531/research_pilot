# ResearchPilot

ResearchPilot is a local-first, skill-driven multimodal research agent. P0-P9 form a complete loop: literature search, PDF workspace, evidence-backed synthesis, human-reviewed experiment design, versioned artifacts, recovery/trace, and reproducible release evaluation.

## Requirements

- Windows PowerShell
- Python 3.12
- Ollama
- A local model; development currently uses `qwen3:14b`

## Setup

```powershell
cd D:\Agent\research-pilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set the exact model tag shown by `ollama list`:

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:14b
OLLAMA_TIMEOUT_SECONDS=300
```

Start Ollama if it is not already running, then confirm its API is available:

```powershell
ollama list
Invoke-RestMethod http://localhost:11434/api/tags
```

## Run the API

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

OpenAPI documentation is available at <http://127.0.0.1:8000/docs>.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Model check:

```powershell
$body = @{ prompt = "Confirm model connectivity" } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/models/test `
  -ContentType application/json `
  -Body $body
```

Minimal research Graph:

```powershell
$body = @{
  research_question = "调研 2024-2026 年 RGB-LWIR image registration"
  keywords = @("RGB-LWIR", "registration")
  year_from = 2024
  year_to = 2026
  maximum_papers = 15
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/research/test `
  -ContentType application/json `
  -Body $body
```

Literature search workflow:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/research/search `
  -ContentType application/json `
  -Body $body
```

This endpoint runs request understanding, generation of 3–5 search queries, Literature MCP calls to OpenAlex, DOI/OpenAlex-ID deduplication, lexical ranking, Qwen relevance ranking, and final paper selection. Current OpenAlex documentation requires a free API key; set `OPENALEX_API_KEY` in `.env`. Check configuration with `GET /health/dependencies`.

Run the literature relevance Gold Set:

```powershell
python -m evals.run_literature_eval
```

Run the UTF-8 Chinese request smoke test:

```powershell
python -m scripts.smoke_chinese_request
```

Every response includes `X-Request-ID`. Validation and provider failures use a common error shape with `code`, `message`, `retryable`, and `request_id`.

Persistent project workflow:

```powershell
$project = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/projects `
  -ContentType application/json `
  -Body (@{ name = "Registration review"; request = $body } | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/projects/$($project.id)/research" `
  -ContentType application/json `
  -Body (@{ run_id = "demo-run-1" } | ConvertTo-Json)
```

Projects, selected papers, execution traces, and LangGraph checkpoints are stored in `DATABASE_PATH`. Use `GET /projects/{id}`, `/papers`, and `/trace` to inspect them; list endpoints accept `limit` and `offset`. A failed workflow is continued with `POST /projects/{id}/resume`. Reusing a completed `run_id` is idempotent.

Project research is orchestrated through a structured Coordinator → Literature Researcher handoff. The researcher loads `systematic-search` only when generating queries and `paper-screening` only when ranking candidates. Skill files are restricted to `SKILLS_ROOT`, size-limited by `SKILL_MAX_BYTES`, cached after first use, and never selected through an arbitrary filesystem path. The project trace exposes `agent_plan`, `agent_handoff`, `skill_load`, `tool_call`, and `agent_return` without storing complete prompts.

PDFs are imported through `WorkspaceManager`, then parsed through the Document MCP. The five tools are `parse_document`, `get_page`, `get_document_structure`, `extract_figures`, and `get_figure`. Artifacts stay below `WORKSPACE_ROOT/{project_id}` and API/MCP models expose only project-relative paths. P4 validation results and current OCR/vector limitations are recorded in [docs/P4_VALIDATION.md](docs/P4_VALIDATION.md).

Evidence is project/paper/document scoped. Upload a PDF with `POST /projects/{project_id}/documents/import`, create text, figure, or table evidence under `/evidence`, save evidence-backed summaries with `PUT /summaries/{paper_id}`, and retrieve the cross-paper matrix from `GET /comparison`. `GET /evidence/{id}/source` revalidates the stored locator and hash before returning a source preview. P5 integrity metrics are recorded in [docs/P5_VALIDATION.md](docs/P5_VALIDATION.md).

Evidence-grounded experiment proposals are created with `POST /projects/{project_id}/experiment-proposal`. The graph checkpoints at `human_approval`; submit an `accept`, restricted `modify`, or `reject` decision to `/experiment-proposal/decision` with the current proposal version. The same approval resumes after a process restart through SQLite Checkpointer. Local Qwen proposal generation can take about four minutes, so the default Ollama timeout is 300 seconds. See [docs/P6_VALIDATION.md](docs/P6_VALIDATION.md).

Generate versioned Markdown, CSV, and Mermaid outputs under `/projects/{project_id}/artifacts`; every download is SHA-256 verified. Start the UI with `streamlit run ui/app.py`. It provides research creation, progress/trace, Evidence, experiment approval, and Artifact preview/download without direct database access. See [docs/P7_VALIDATION.md](docs/P7_VALIDATION.md).

Long-running literature work persists each query item independently. Resume skips completed items and retries only failed work. Inspect `/projects/{id}/progress`, `/progress/metrics`, `/trace`, and `/trace/metrics`; Trace supports event type and success filters. The same `X-Request-ID` crosses API, Graph metadata, MCP, and OpenAlex boundaries. Reliability and security results are in [docs/P8_VALIDATION.md](docs/P8_VALIDATION.md).

## Quality checks

```powershell
python -m pytest -q
python -m ruff check .
python -m pip check
```

Release evaluation and fixed offline demo:

```powershell
python -m evals.run_researchpilot_eval
python -m scripts.run_fixed_demo
```

The 20-item suite is a frozen deterministic regression snapshot, not a fresh online model benchmark. See [P9 release validation](docs/P9_RELEASE_VALIDATION.md) for interpretation and clean-install evidence, and [architecture](docs/ARCHITECTURE.md) for component boundaries.

## Troubleshooting

- `MODEL_UNAVAILABLE`: check that Ollama is running, the configured model exists, and `OLLAMA_BASE_URL` is reachable.
- Model not found: copy the exact name from `ollama list` into `.env`.
- Timeout during the first call: model loading may take longer; increase `OLLAMA_TIMEOUT_SECONDS` if necessary.
- PowerShell blocks activation: run `Set-ExecutionPolicy -Scope Process Bypass`, then activate the environment again.

## Current scope

P0 through P9 are implemented. The detailed implementation sequence is in [docs/DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md).

Known unresolved problems and their closing criteria are tracked in [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).
