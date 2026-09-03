import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from app.documents.acquisition import OpenAccessDownloader
from app.documents.errors import DocumentSecurityError, DocumentValidationError
from app.documents.service import DocumentService
from app.llm import OllamaProvider
from app.schemas import (
    ClaimValue,
    EvidenceNode,
    MethodCardDraft,
    PaperAnalysisJob,
    PaperHandle,
    PaperSummary,
    SupportedMethodField,
    VisualObservation,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaperIntelligenceService:
    """Portable, persistent paper-understanding backend for the public Document MCP."""

    def __init__(
        self, documents: DocumentService, state_path: str | Path,
        allowed_roots: list[str | Path] | None = None, allow_local_files: bool = True,
    ) -> None:
        self.documents = documents
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = [Path(value).resolve() for value in (allowed_roots or [Path.cwd()])]
        self.allow_local_files = allow_local_files
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        state = self._load()
        changed = False
        for job_id, value in state["jobs"].items():
            if value["status"] in {"queued", "running"}:
                value["status"] = "failed"
                value["error"] = {
                    "type": "AnalysisInterrupted",
                    "message": "Server restarted before analysis completed; start a new job.",
                }
                state["jobs"][job_id] = value
                changed = True
        if changed:
            self._save(state)

    def _load(self) -> dict:
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            papers = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT handle, payload_json FROM paper_sessions")
            }
            jobs = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT job_id, payload_json FROM analysis_jobs")
            }
        return {"papers": papers, "jobs": jobs}

    def _save(self, value: dict) -> None:
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            connection.executemany(
                "INSERT OR REPLACE INTO paper_sessions(handle,payload_json) VALUES (?,?)",
                [(key, json.dumps(item, ensure_ascii=False))
                 for key, item in value["papers"].items()],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO analysis_jobs(job_id,paper_handle,status,payload_json) "
                "VALUES (?,?,?,?)",
                [(key, item["paper_handle"], item["status"],
                  json.dumps(item, ensure_ascii=False)) for key, item in value["jobs"].items()],
            )
            connection.commit()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS paper_sessions (
                handle TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                job_id TEXT PRIMARY KEY,
                paper_handle TEXT NOT NULL REFERENCES paper_sessions(handle) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_paper
            ON analysis_jobs(paper_handle,status);
        """)

    def _local_path(self, uri: str) -> Path:
        if not self.allow_local_files:
            raise DocumentSecurityError("Local files are disabled for HTTP MCP")
        parsed = urlparse(uri)
        raw = unquote(parsed.path)
        if parsed.netloc:
            raw = f"//{parsed.netloc}{raw}"
        if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        path = Path(raw).resolve()
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise DocumentSecurityError("Paper path is outside configured MCP roots")
        return path

    async def submit(self, source_uri: str) -> PaperHandle:
        parsed = urlparse(source_uri)
        if parsed.scheme == "file":
            path = self._local_path(source_uri)
            content, name = path.read_bytes(), path.name
        elif parsed.scheme == "https":
            content, final_url = await OpenAccessDownloader(
                self.documents.workspace.max_document_bytes
            ).fetch(source_uri)
            name = Path(urlparse(final_url).path).name or "paper.pdf"
        else:
            raise DocumentSecurityError("Only file:// and https:// paper sources are supported")
        handle = str(uuid4())
        project_id = f"mcp-paper-{handle}"
        entry = self.documents.workspace.import_pdf_bytes(project_id, name, content)
        self.documents.parse_document(project_id, entry.document_id)
        async with self._lock:
            state = self._load()
            state["papers"][handle] = {
                "project_id": project_id, "document_id": entry.document_id,
                "sha256": entry.sha256, "source_path": entry.relative_path,
                "status": "parsed", "created_at": _now(),
            }
            self._save(state)
        return PaperHandle(paper_handle=handle, status="parsed")

    def _paper(self, handle: str) -> dict:
        paper = self._load()["papers"].get(handle)
        if not paper:
            raise DocumentValidationError("Unknown paper handle")
        return paper

    def structure(self, handle: str):
        paper = self._paper(handle)
        return self.documents.get_structure(paper["project_id"], paper["document_id"])

    async def start(self, handle: str, objectives: list[str] | None = None) -> PaperAnalysisJob:
        self._paper(handle)
        job_id = str(uuid4())
        job = PaperAnalysisJob(
            analysis_job_id=job_id, paper_handle=handle, status="queued",
            progress={"completed_items": 0, "total_items": 1},
        )
        async with self._lock:
            state = self._load()
            state["jobs"][job_id] = job.model_dump(mode="json")
            self._save(state)
        self._tasks[job_id] = asyncio.create_task(
            self._analyze(job_id, handle, objectives or []), name=f"paper-analysis-{job_id}"
        )
        return job

    async def _analyze(self, job_id: str, handle: str, objectives: list[str]) -> None:
        await self._update_job(job_id, status="running")
        try:
            parsed = self.structure(handle)
            paper = self._paper(handle)
            evidence = []
            for page in [value for value in parsed.pages if value.text.strip()][:8]:
                excerpt = page.text.strip()[:1_000]
                evidence.append(EvidenceNode(
                    evidence_id=str(uuid4()), project_id=paper["project_id"],
                    paper_id=handle, document_id=paper["document_id"], evidence_type="text",
                    claim="Directly extracted paper passage", confidence=1,
                    page_number=page.page_number, excerpt=excerpt, span_start=0,
                    span_end=len(excerpt), source_path=paper["source_path"],
                    source_hash=paper["sha256"], created_at=_now(),
                ))
            async with OllamaProvider() as provider:
                for kind, regions in (("figure", parsed.figures), ("table", parsed.tables)):
                    for region in regions:
                        if not region.source_path or not region.sha256:
                            continue
                        path = self.documents.workspace.resolve_safe_path(
                            paper["project_id"], region.source_path
                        )
                        observation = await provider.structured_output_with_images([{
                            "role": "user",
                            "content": "Describe only visible academic-paper facts. "
                                f"Type={kind}; caption={region.caption or 'unknown'}",
                        }], [path.read_bytes()], VisualObservation)
                        evidence.append(EvidenceNode(
                            evidence_id=str(uuid4()), project_id=paper["project_id"],
                            paper_id=handle, document_id=paper["document_id"],
                            evidence_type=kind, claim=observation.summary,
                            confidence=observation.confidence, page_number=region.page_number,
                            label=region.label or f"{kind}-{region.page_number}", bbox=region.bbox,
                            source_path=region.source_path, source_hash=region.sha256,
                            created_at=_now(),
                        ))
                index = "\n".join(
                    f"{item.evidence_id} [{item.evidence_type}, page {item.page_number}]: {item.claim}"
                    for item in evidence
                )
                draft = await provider.structured_output([
                    {"role": "system", "content": (
                        "Create a domain-neutral method card. supported_fact values must cite only "
                        "the supplied evidence IDs; all other claims must be inference."
                    )},
                    {"role": "user", "content": f"OBJECTIVES: {objectives}\nEVIDENCE:\n{index}"},
                ], MethodCardDraft)
            allowed = {item.evidence_id for item in evidence}
            for field in self._method_fields(draft):
                if not set(field.evidence_ids) <= allowed:
                    raise DocumentValidationError("Method card cited unknown evidence")
            visual = next((item for item in evidence if item.evidence_type != "text"), None)
            if visual and not any(visual.evidence_id in field.evidence_ids
                                  for field in self._method_fields(draft)):
                draft.key_components.append(SupportedMethodField(
                    value=visual.claim, kind="supported_fact", evidence_ids=[visual.evidence_id]
                ))
            method = draft.model_dump(mode="json")
            contributions = [ClaimValue(
                value=value.value,
                kind="supported" if value.kind == "supported_fact" else "inference",
                evidence_ids=value.evidence_ids,
            ) for value in draft.strengths]
            if visual and not any(visual.evidence_id in item.evidence_ids for item in contributions):
                contributions.append(ClaimValue(
                    value=visual.claim, kind="supported", evidence_ids=[visual.evidence_id]
                ))
            summary = PaperSummary(
                summary_id=str(uuid4()), project_id=paper["project_id"], paper_id=handle,
                method=[ClaimValue(
                    value=draft.method_name.value,
                    kind="supported" if draft.method_name.kind == "supported_fact" else "inference",
                    evidence_ids=draft.method_name.evidence_ids,
                )],
                contributions=contributions,
                limitations=[ClaimValue(
                    value=value.value,
                    kind="supported" if value.kind == "supported_fact" else "inference",
                    evidence_ids=value.evidence_ids,
                ) for value in draft.limitations], created_at=_now(),
            )
            await self._update_job(job_id, status="completed", progress={
                "completed_items": 1, "total_items": 1,
            }, result={
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "method_card": method, "summary": summary.model_dump(mode="json"),
            })
        except Exception as exc:  # noqa: BLE001 - persistent job records its boundary
            await self._update_job(job_id, status="failed", error={
                "type": type(exc).__name__, "message": str(exc)[:1_000],
            })

    @staticmethod
    def _method_fields(card: MethodCardDraft):
        return [card.method_name, card.target_problem, *card.mechanism_steps, *card.inputs,
                *card.outputs, *card.key_components, *card.assumptions,
                *card.resource_requirements, *card.evaluation_context, *card.strengths,
                *card.limitations]

    async def _update_job(self, job_id: str, **updates) -> None:
        async with self._lock:
            state = self._load()
            current = PaperAnalysisJob.model_validate(state["jobs"][job_id])
            state["jobs"][job_id] = current.model_copy(update=updates).model_dump(mode="json")
            self._save(state)

    def job(self, job_id: str) -> PaperAnalysisJob:
        value = self._load()["jobs"].get(job_id)
        if not value:
            raise DocumentValidationError("Unknown analysis job")
        return PaperAnalysisJob.model_validate(value)

    def result_part(self, handle: str, key: str):
        jobs = [PaperAnalysisJob.model_validate(value) for value in self._load()["jobs"].values()
                if value["paper_handle"] == handle and value["status"] == "completed"]
        if not jobs or not jobs[-1].result:
            raise DocumentValidationError("Paper analysis is not complete")
        return jobs[-1].result[key]
