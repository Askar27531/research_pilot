"""Flow-coverage probe: run ONE simple research+analysis flow end to end with
real Ollama models (offline fake literature + one synthetic PDF) while tracing
which app/ functions actually execute. Then list functions that were NEVER
executed, split into "statically referenced (dormant/branch)" vs "zero static
references anywhere" — the latter are the strongest real-dead-code candidates.

Usage:  .venv\\Scripts\\python.exe scripts\\flow_coverage_probe.py
Needs:  local Ollama running with the text model from .env (default qwen3:latest)
        ~3-8 minutes. Writes data/flow_coverage_report.txt
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import types
import uuid
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.db import (  # noqa: E402
    Database,
    DocumentRepository,
    HitlEventRepository,
    PaperRepository,
    ProjectRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    TraceRepository,
    TransferRepository,
    WorkflowJobRepository,
    WorkItemRepository,
)
from app.db.repositories import utc_now  # noqa: E402
from app.documents import DocumentService, PDFParser, WorkspaceManager  # noqa: E402
from app.schemas import (  # noqa: E402
    PaperAcquisition,
    PaperAuthor,
    PaperMetadata,
    ResearchRequest,
    SearchResult,
)
from app.services import ResearchWorkflowService  # noqa: E402
from app.skills import SkillRegistry  # noqa: E402

settings = get_settings()
REPORT = ROOT / "data" / "flow_coverage_report.txt"


# ----------------------------------------------------------------------------
# 1. fake offline literature + fake document capability
# ----------------------------------------------------------------------------
class FakeLiterature:
    """Stand-in for the MCP literature capability client (retrieval only)."""

    def __init__(self, papers: list[PaperMetadata]) -> None:
        self._papers = papers

    async def search_papers(
        self, query, year_from=None, year_to=None, limit=20, sources=None, trace_id=None
    ):
        return SearchResult(
            query=query, papers=self._papers, total_available=len(self._papers),
            warnings=[], source_latency_ms=1,
        )


class FakeCapabilities:
    """Offline DocumentCapabilityClient stand-in (parse on the real service)."""

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    async def parse_document(self, project_id: str, document_id: str):
        return self.service.parse_document(project_id, document_id)


# ----------------------------------------------------------------------------
# 2b. deterministic fake LLM provider (valid minimal outputs per model type)
# ----------------------------------------------------------------------------
class FakeProvider:
    """Offline stand-in for OllamaProvider: returns schema-valid minimal objects.

    Model output quality is irrelevant here; the probe measures which of our
    own functions the real flow wiring touches, not what a model would say.
    """

    def __init__(self, usage_sink=None) -> None:  # noqa: D401
        self._usage_sink = usage_sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        return None

    @staticmethod
    def _claim(value: str) -> dict:
        return {"value": value, "kind": "inference", "evidence_ids": []}

    def _visual_ids(self, messages) -> list[str]:
        """Figure/table evidence ids present in the last user message."""
        try:
            content = messages[-1]["content"] if messages else ""
            return re.findall(
                r'"evidence_id":\s*"([0-9a-f-]+)"[^\n]*?"type":\s*"(?:figure|table)"',
                content,
            )
        except Exception:
            return []

    async def _emit(self, kind: str = "text") -> None:
        if self._usage_sink is None:
            return
        try:
            await self._usage_sink(types.SimpleNamespace(
                kind=kind, model="fake-probe", prompt_tokens=700,
                completion_tokens=700, latency_ms=5,
            ))
        except Exception:
            pass

    def _build(self, response_model, messages=None) -> object:
        name = response_model.__name__
        if name == "ResearchUnderstanding":
            return response_model(
                normalized_goal="图像分类的卷积网络基线效果",
                core_concepts=["图像分类", "卷积网络"],
                domain="计算机视觉",
            )
        if name == "SearchQueryPlan":
            concepts = ["图像分类", "卷积网络", "基线"]
            return response_model(
                topic="图像分类基线",
                required_concept_groups=[["图像分类"]],
                excluded_topics=[],
                queries=[
                    {"query": "图像分类 卷积网络 准确率", "purpose": "core",
                     "concepts": concepts[:1]},
                    {"query": "CNN image classification baseline", "purpose": "synonym",
                     "concepts": concepts},
                    {"query": "图像分类 方法 对比", "purpose": "method",
                     "concepts": concepts[:2]},
                ],
            )
        if name == "PaperScreeningBatch":
            ids = []
            try:
                content = messages[-1]["content"] if messages else ""
                idx = content.rfind("Papers:")
                payload = content[idx + len("Papers:"):].strip()
                items = json.loads(payload)
                ids = [item["stable_id"] for item in items]
            except Exception:
                ids = []
            return response_model(decisions=[
                {"stable_id": sid, "include": True, "relevance": 90,
                 "reason": "probe: include all", "matched_required_concepts": []}
                for sid in ids
            ])
        if name == "ProblemAnalysisDraft":
            visuals = self._visual_ids(messages)
            core: list[dict] = [self._claim("问题：用简单卷积网络做图像分类基线")]
            if visuals:
                core.append({"value": "实验：图显示准确率随训练上升至约 89%",
                             "kind": "supported", "evidence_ids": [visuals[0]]})
            return response_model(
                core_problem=core,
                relevance_to_topic=[self._claim("相关：与图像分类课题直接相关")],
            )
        if name == "MethodAnalysisDraft":
            return response_model(
                methods=[self._claim("方法：两层卷积加全连接，ReLU 与池化")],
                mechanisms=[self._claim("机制：卷积提取局部特征后分类")],
            )
        if name == "ExperimentAnalysisDraft":
            return response_model(
                experimental_setup=[self._claim("实验：公开数据集十轮训练")],
                main_results=[self._claim("结果：验证集准确率约 89%")],
            )
        if name == "CriticalAnalysisDraft":
            return response_model(limitations=[self._claim("局限：小数据短训练，不可直接对比 SOTA")])
        if name == "PaperOverviewDraft":
            return response_model(overview=self._claim("总览：本文实现并评估了一个图像分类 CNN 基线"))
        if name == "ComparisonDraft":
            return response_model()
        if name == "VisualObservation":
            return response_model(
                figure_type="result", summary="图中曲线显示验证集准确率随训练轮次上升",
                observations=["横轴为轮次，纵轴为准确率，曲线单调上升"],
                main_results=["最终准确率约 89%"], unknowns=[], confidence=0.9,
            )
        if name == "BlindVisualFacts":
            return response_model(
                visible_facts=["图像含一条随轮次上升的准确率曲线，终点约 89%"],
                unknowns=["数据集细节无法从图读出"],
            )
        if name == "VisualVerificationVerdict":
            return response_model(
                status="confirmed", reason="probe: 图与论断一致", confidence=0.9,
                regions=[],
            )
        if name == "CrossModalConsistencyReport":
            return response_model(checks=[
                {"mention_index": 1, "status": "consistent",
                 "visible_evidence": "曲线与正文表述一致", "note": ""},
            ])
        raise TypeError(f"FakeProvider does not know model {name}")

    async def chat(self, messages):
        return "probe"

    async def structured_output(self, messages, response_model):
        obj = self._build(response_model, messages)
        await self._emit()
        return obj

    async def structured_output_bounded(self, messages, response_model, max_output_tokens):
        obj = self._build(response_model, messages)
        await self._emit()
        return obj

    async def structured_output_with_images(self, messages, images, response_model):
        obj = self._build(response_model, messages)
        await self._emit("vision")
        return obj


def _install_fake_provider() -> None:
    """Point ResearchWorkflowService's provider construction at the fake."""
    import app.services.workflow as workflow_mod
    workflow_mod.OllamaProvider = FakeProvider


# ----------------------------------------------------------------------------
# 2. tiny synthetic PDF (one "simple paper", text only -> zero visuals)
# ----------------------------------------------------------------------------
def make_simple_pdf(with_image: bool = False) -> bytes:
    lines = [
        "A Simple Study of Image Classification with Convolutional Networks",
        "",
        "Abstract. Convolutional neural networks achieve strong accuracy on image "
        "classification benchmarks. In this simple study we train a small CNN on a "
        "public dataset and report the classification accuracy on held-out images.",
        "",
        "1 Introduction. Image classification assigns a label to an input image. "
        "Recent models use stacked convolutional layers followed by fully connected "
        "layers. Our goal is to reproduce a small baseline pipeline and evaluate it.",
        "",
        "2 Methods. We use 32x32 grayscale images. The network has two convolutional "
        "layers with ReLU activations, max pooling, and one dense layer. We train for "
        "ten epochs with stochastic gradient descent at learning rate 0.01.",
        "",
        "3 Results. Figure 1 shows the training accuracy rising over epochs. "
        "The model reaches 89% accuracy on the validation split after ten epochs. "
        "Training takes about two minutes on a laptop CPU.",
        "",
        "4 Conclusion. A small convolutional network is a solid baseline for image "
        "classification. Future work may add data augmentation and batch normalization.",
        "",
        "5 Limitations. The study uses a small dataset and a short training budget, so "
        "the reported accuracy is not directly comparable with published state of the art.",
    ]
    doc = fitz.open()
    for i in range(0, len(lines), 14):
        page = doc.new_page()
        y = 60
        for raw in lines[i:i + 14]:
            page.insert_text((60, y), raw[:95], fontsize=11, fontname="helv")
            y += 18
    if with_image:
        page = doc[-1]
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 150))
        rect = fitz.Rect(60, y + 24, 300, y + 174)
        page.insert_image(rect, pixmap=pix)
        page.insert_text((60, y + 186), "Figure 1: Training accuracy over epochs.",
                         fontsize=10, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


# ----------------------------------------------------------------------------
# 3. function-level tracer (records every executed app/ function)
# ----------------------------------------------------------------------------
executed: set[tuple[str, str]] = set()  # (abs_path, qualname)


def _install_tracer() -> None:
    app_root = str(APP).replace("\\", "/")

    def tracer(frame, event, arg):  # noqa: ARG001
        if event == "call":
            code = frame.f_code
            path = code.co_filename.replace("\\", "/")
            if path.startswith(app_root + "/"):
                qn = getattr(code, "co_qualname", None) or code.co_name
                executed.add((path, qn))
        return tracer

    sys.settrace(tracer)
    threading.settrace(tracer)


def _definition_map() -> dict[tuple[str, str], tuple[int, bool]]:
    """abs path -> (line, has_decorator) for every def (funcs + Class.methods)."""
    out: dict[tuple[str, str], tuple[int, bool]] = {}
    for r, _, fs in os.walk(APP):
        if "__pycache__" in r:
            continue
        for f in fs:
            if not f.endswith(".py"):
                continue
            path = os.path.join(r, f).replace("\\", "/")
            try:
                tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[(path, node.name)] = (node.lineno, bool(node.decorator_list))
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out[(path, f"{node.name}.{sub.name}")] = (
                                sub.lineno, bool(sub.decorator_list))
    return out


def _covered(qualname: str, executed_qns: set[str]) -> bool:
    return any(q == qualname or q.startswith(qualname + ".") for q in executed_qns)


def _static_refs_correct(name: str) -> tuple[int, int]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    scan_dirs = ["app", "ui", "mcp_servers", "scripts", "evals"]
    texts: list[tuple[str, str]] = []
    for d in scan_dirs:
        base = ROOT / d
        if not base.is_dir():
            continue
        for r, _, fs in os.walk(base):
            if "__pycache__" in r:
                continue
            for f in fs:
                if f.endswith(".py"):
                    p = os.path.join(r, f).replace("\\", "/")
                    texts.append((p, Path(p).read_text(encoding="utf-8", errors="replace")))
    external = internal = 0
    for path, text in texts:
        n = len(pattern.findall(text))
        if n == 0:
            continue
        if path.startswith(str(APP).replace("\\", "/") + "/"):
            internal += n
        else:
            external += n
    return external, internal


# ----------------------------------------------------------------------------
# 4. main flow
# ----------------------------------------------------------------------------
async def run_flow(pdf_bytes: bytes | None = None) -> dict:
    for leftover in list((ROOT / "data").glob("covprobe_*")):
        try:
            shutil.rmtree(leftover, ignore_errors=True)
        except Exception:
            pass
    tmp = Path(tempfile.mkdtemp(prefix="covprobe_", dir=str(ROOT / "data")))
    db_path = tmp / "probe.db"
    ws_root = tmp / "workspaces"
    ws_root.mkdir(parents=True)

    database = Database(db_path)
    await database.initialize()
    conn = await aiosqlite_connect(db_path)
    checkpointer = make_checkpointer(conn)
    await checkpointer.setup()

    workspace = WorkspaceManager(ws_root, max_document_bytes=settings.document_max_bytes)
    parser = PDFParser(workspace, render_dpi=settings.pdf_render_dpi,
                       ocr_languages=settings.ocr_languages)
    doc_service = DocumentService(workspace, parser)
    skills = SkillRegistry(Path(settings.skills_root), max_bytes=settings.skill_max_bytes)
    skills.discover()

    state = types.SimpleNamespace(
        database=database,
        checkpointer=checkpointer,
        skill_registry=skills,
        document_service=doc_service,
        document_capabilities=FakeCapabilities(doc_service),
        workflow_worker=types.SimpleNamespace(wake=lambda: None),
    )
    app_like = types.SimpleNamespace(state=state)

    fake_paper = PaperMetadata(
        stable_id="https://doi.org/10.1234/simple.cnn.2020",
        source_id="probe-1",
        title="A Simple Study of Image Classification with Convolutional Networks",
        authors=[PaperAuthor(name="Probe Author")],
        year=2020,
        abstract="A minimal convolutional baseline for image classification.",
        doi="10.1234/simple.cnn.2020",
        source="openalex",
        sources=["openalex"],
    )
    service = ResearchWorkflowService(app_like, FakeLiterature([fake_paper]))

    # --- simple research request -------------------------------------------------
    request = ResearchRequest(
        research_question="图像分类用卷积网络基线效果如何？",
        keywords=["图像分类", "卷积网络"],
        year_from=2018, year_to=2025, maximum_papers=1,
        literature_sources=["openalex"],
    )
    projects = ProjectRepository(database)
    research = ResearchDataRepository(database)
    sessions = ResearchSessionRepository(database)
    jobs = WorkflowJobRepository(database)
    papers_repo = PaperRepository(database)
    documents_repo = DocumentRepository(database)
    transfers = TransferRepository(database)

    project_id, first_job_id = await research.create_workspace(
        "Probe 简单图像分类", request
    )
    await sessions.begin_search(project_id)
    print(f"[flow] project={project_id[:8]} job1={first_job_id[:8]}", flush=True)

    job1 = await jobs.claim_next()
    await service.execute_job(job1)  # search graph (3 small LLM calls)
    after_research = await projects.get(project_id)
    print(f"[flow] research done; project status="
          f"{after_research.status}/{after_research.current_stage}", flush=True)

    # --- "human" selects paper --------------------------------------------------
    current = await sessions.current_search(project_id)
    revision = current["revision"]
    listed = await papers_repo.list_for_project(project_id)
    chosen = listed[0]
    await sessions.select(project_id, revision, [chosen.id], requirements=None)

    # --- "human" uploads the synthetic PDF + marks parsed ------------------------
    pdf_bytes = pdf_bytes or make_simple_pdf()
    entry = workspace.import_pdf_bytes(project_id, "simple.pdf", pdf_bytes)
    await documents_repo.register(project_id, chosen.id, entry)
    await transfers.upsert_acquisition(PaperAcquisition(
        project_id=project_id, paper_id=chosen.id, status="parsed",
        document_id=entry.document_id, updated_at=utc_now(),
    ))
    print(f"[flow] selected 1 paper, uploaded {len(pdf_bytes)} bytes", flush=True)

    # --- run the analysis job (fake LLM: 5 specialist calls + index) ----------
    await jobs.succeed(job1.job_id)  # mimic the worker: research job ends first
    await jobs.enqueue(project_id, "document_analysis")  # "human action" queues it
    job2 = await jobs.claim_next()
    print(f"[flow] analysis job {job2.job_id[:8]} (type={job2.job_type})", flush=True)
    await service.execute_job(job2)
    final = await projects.get(project_id)
    print(f"[flow] done: project status={final.status} stage={final.current_stage}",
          flush=True)

    report = await sessions.report(project_id, revision)
    print(f"[flow] report saved: {report is not None}", flush=True)

    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return {"project_id": project_id}


def aiosqlite_connect(db_path: Path):
    import aiosqlite
    return aiosqlite.connect(str(db_path))


def make_checkpointer(conn):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return AsyncSqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))


# ----------------------------------------------------------------------------
# 5. coverage report
# ----------------------------------------------------------------------------
def write_report(failed: str | None = None) -> None:
    defs = _definition_map()
    executed_qns = {q for _, q in executed}
    never: list[tuple[str, str, int, bool]] = []
    for (path, qn), (line, has_deco) in defs.items():
        if not _covered(qn, executed_qns):
            never.append((path, qn, line, has_deco))

    # out of probe scope: HTTP routes / MCP gateway layers (not driven here)
    def in_scope(path: str) -> bool:
        rel = os.path.relpath(path, APP).replace("\\", "/")
        return not (rel.startswith("api/") or rel.startswith("mcp/"))

    never_in_scope = [item for item in never if in_scope(item[0])]
    never_in_scope.sort()

    zero_refs: list[tuple[str, str, int]] = []
    dormant: list[tuple[str, str, int]] = []
    for path, qn, line, has_deco in never_in_scope:
        name = qn.split(".")[-1]
        external, internal = _static_refs_correct(name)
        if external == 0 and internal <= 1 and not has_deco:
            zero_refs.append((path, qn, line))
        else:
            dormant.append((path, qn, line))

    out_of_scope = [item for item in never if not in_scope(item[0])]
    lines = []
    lines.append("ResearchPilot flow-coverage probe")
    lines.append(f"app 定义数: {len(defs)} ; 执行过的不同函数数: {len(executed_qns)}")
    lines.append(f"从未执行的定义数: {len(never)}"
                 f" (其中 HTTP/MCP 层未驱动 {len(out_of_scope)})")
    lines.append("")
    lines.append("【A 类：未执行、无装饰器、静态零引用 —— 真死代码候选】")
    for path, qn, line in zero_refs:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        lines.append(f"  {rel}:{line}  {qn}")
    lines.append("")
    lines.append("【B 类：未执行但有静态引用/装饰器 —— 休眠功能或需特定条件】")
    for path, qn, line in dormant[:200]:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        lines.append(f"  {rel}:{line}  {qn}")
    if len(dormant) > 200:
        lines.append(f"  ... 其余 {len(dormant) - 200} 个")
    if failed:
        lines.append("")
        lines.append(f"注：流程在完成前失败 —— {failed}（覆盖为部分结果）")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[report] {REPORT}")
    print(f"defs={len(defs)} executed={len(executed_qns)} never={len(never)} "
          f"scope未执行={len(never_in_scope)} A零引用={len(zero_refs)} B休眠={len(dormant)}")
    print("--- A 类候选（核心层：未执行 + 无装饰器 + 静态零引用）---")
    for path, qn, line in zero_refs[:60]:
        print(f"  {os.path.relpath(path, ROOT)}:{line}  {qn}")


# ----------------------------------------------------------------------------
# 6. strengthened tiers: visuals / HTTP layer / HITL + budget gate
# ----------------------------------------------------------------------------
def _reset_settings() -> None:
    from app.core import config as _cfg
    _cfg.get_settings.cache_clear()


def _fake_paper() -> PaperMetadata:
    return PaperMetadata(
        stable_id="https://doi.org/10.1234/simple.cnn.2020",
        source_id="probe-1",
        title="A Simple Study of Image Classification with Convolutional Networks",
        authors=[PaperAuthor(name="Probe Author")],
        year=2020,
        abstract="A minimal convolutional baseline for image classification.",
        doi="10.1234/simple.cnn.2020",
        source="openalex",
        sources=["openalex"],
    )


def _request() -> ResearchRequest:
    return ResearchRequest(
        research_question="图像分类用卷积网络基线效果如何？",
        keywords=["图像分类", "卷积网络"],
        year_from=2018, year_to=2025, maximum_papers=1,
        literature_sources=["openalex"],
    )


async def s3_http_layer() -> list[str]:
    """Tier 2: drive the real FastAPI app through ASGI (httpx TestClient)."""
    notes = ["== HTTP 层 =="]
    tmp = Path(tempfile.mkdtemp(prefix="covprobe_http_", dir=str(ROOT / "data")))
    db = tmp / "http.db"
    ws = tmp / "ws"
    ws.mkdir()
    database = Database(db)
    await database.initialize()
    os.environ["DATABASE_PATH"] = str(db)
    os.environ["WORKSPACE_ROOT"] = str(ws)
    _reset_settings()
    from fastapi.testclient import TestClient  # noqa: PLC0415
    import app.main as main_mod  # noqa: PLC0415
    app = main_mod.app
    try:
        with TestClient(app) as client:
            cases = [
                ("GET /projects", lambda: client.get("/projects")),
                ("GET 404 project", lambda: client.get(
                    "/projects/00000000-0000-0000-0000-000000000000")),
                ("POST invalid (422)", lambda: client.post("/projects", json={})),
                ("GET /mcp/status", lambda: client.get("/mcp/status")),
                ("GET bad resource token", lambda: client.get(
                    "/projects/00000000-0000-0000-0000-000000000000/resources/bad-token")),
            ]
            for name, fn in cases:
                try:
                    r = fn()
                    notes.append(f"{name} -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{name} ERR {type(exc).__name__}: {exc}")
            body = {
                "name": "probe-http",
                "research_question": "HTTP 层覆盖用简单课题",
                "current_approach": "读论文",
                "difficulties": ["缺少文献"],
                "target_metrics": ["覆盖率"],
                "advanced": {"year_from": 2019, "year_to": 2025,
                             "max_papers": 1, "sources": ["openalex"]},
            }
            r = client.post("/projects", json=body)
            notes.append(f"POST /projects valid -> {r.status_code}")
            pid = None
            try:
                pid = (r.json().get("project") or {}).get("id")
            except Exception:
                pass
            if not pid:
                try:
                    rows = client.get("/projects").json().get("projects", [])
                    pid = rows[0].get("id") if rows else None
                except Exception:
                    pid = None
            if pid:
                notes.append(f"created project {pid[:8]}")
                try:
                    r = client.patch(f"/projects/{pid}",
                                     json={"research_question": "更新后的课题"})
                    notes.append(f"PATCH /projects/{{id}} -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"PATCH ERR {exc}")
                try:
                    r = client.get(f"/projects/{pid}/workspace")
                    notes.append(f"GET workspace -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"workspace ERR {exc}")
                try:
                    r = client.delete(f"/projects/{pid}")
                    notes.append(f"DELETE /projects/{{id}} -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"DELETE ERR {exc}")
    finally:
        _reset_settings()
        for key in ("DATABASE_PATH", "WORKSPACE_ROOT"):
            os.environ.pop(key, None)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return notes


async def s4_hitl_budget() -> list[str]:
    """Tier 3: selection-gate HITL park + worker guard + budget-gate pause."""
    notes = ["== HITL + 预算门槛 =="]
    tmp = Path(tempfile.mkdtemp(prefix="covprobe_hb_", dir=str(ROOT / "data")))
    db_path = tmp / "hb.db"
    ws_root = tmp / "ws"
    ws_root.mkdir()
    try:
        database = Database(db_path)
        await database.initialize()
        conn = await aiosqlite_connect(db_path)
        cp = make_checkpointer(conn)
        await cp.setup()
        workspace = WorkspaceManager(ws_root, max_document_bytes=settings.document_max_bytes)
        parser = PDFParser(workspace, render_dpi=settings.pdf_render_dpi,
                           ocr_languages=settings.ocr_languages)
        doc_service = DocumentService(workspace, parser)
        skills = SkillRegistry(Path(settings.skills_root),
                               max_bytes=settings.skill_max_bytes)
        skills.discover()
        state = types.SimpleNamespace(
            database=database, checkpointer=cp, skill_registry=skills,
            document_service=doc_service,
            document_capabilities=FakeCapabilities(doc_service),
            workflow_worker=types.SimpleNamespace(wake=lambda: None),
        )
        service = ResearchWorkflowService(types.SimpleNamespace(state=state),
                                          FakeLiterature([_fake_paper()]))
        research = ResearchDataRepository(database)
        sessions = ResearchSessionRepository(database)
        jobs = WorkflowJobRepository(database)
        projects = ProjectRepository(database)
        papers_repo = PaperRepository(database)
        docs_repo = DocumentRepository(database)
        transfers = TransferRepository(database)
        hitl = HitlEventRepository(database)

        pid, _ = await research.create_workspace("Probe hitl/budget", _request())
        await sessions.begin_search(pid)
        j = await jobs.claim_next()
        await service.execute_job(j)
        await jobs.succeed(j.job_id)
        notes.append("research ok")

        # (a) analysis run WITHOUT selection -> select gate parks (interrupt)
        await jobs.enqueue(pid, "document_analysis")
        j2 = await jobs.claim_next()
        await service.execute_job(j2)
        await jobs.succeed(j2.job_id)
        proj = await projects.get(pid)
        notes.append(f"gate park: status={proj.status} stage={proj.current_stage} "
                     f"open={await hitl.has_open(pid)}")

        # (b) worker guard: queued job while an open event exists is skipped
        await jobs.enqueue(pid, "document_analysis")
        j3 = await jobs.claim_next()
        await service.execute_job(j3)
        await jobs.succeed(j3.job_id)
        notes.append(f"guard skip ok, still open={await hitl.has_open(pid)}")

        # (c) human resolves, picks the paper, uploads the PDF
        await hitl.resolve_all(pid, resolved_by="probe",
                               resolution={"action": "select_papers"})
        current = await sessions.current_search(pid)
        revision = current["revision"]
        chosen = (await papers_repo.list_for_project(pid))[0]
        await sessions.select(pid, revision, [chosen.id], requirements=None)
        entry = workspace.import_pdf_bytes(pid, "simple.pdf", make_simple_pdf())
        await docs_repo.register(pid, chosen.id, entry)
        await transfers.upsert_acquisition(PaperAcquisition(
            project_id=pid, paper_id=chosen.id, status="parsed",
            document_id=entry.document_id, updated_at=utc_now()))

        # (d) budget gate: tiny threshold -> analysis must pause at a boundary
        os.environ["BUDGET_GATE_TOKENS"] = "1000"
        _reset_settings()
        await jobs.enqueue(pid, "document_analysis")
        j4 = await jobs.claim_next()
        await service.execute_job(j4)
        await jobs.succeed(j4.job_id)
        proj = await projects.get(pid)
        notes.append(f"budget pause: status={proj.status} reason={proj.pause_reason}")
        assert proj.status == "waiting" and proj.pause_reason == "budget_gate"

        # (e) human "continue": ack the gate, then finish with a huge budget
        await research.ack_budget_gate(pid)
        os.environ["BUDGET_GATE_TOKENS"] = "100000000"
        _reset_settings()
        await jobs.enqueue(pid, "document_analysis")
        j5 = await jobs.claim_next()
        await service.execute_job(j5)
        await jobs.succeed(j5.job_id)
        proj = await projects.get(pid)
        notes.append(f"resume after ack: status={proj.status}")
        os.environ.pop("BUDGET_GATE_TOKENS", None)
        _reset_settings()
    except Exception as exc:  # noqa: BLE001 - keep coverage rolling
        import traceback
        traceback.print_exc()
        notes.append(f"HITL/budget FAILED {type(exc).__name__}: {exc}")
    finally:
        _reset_settings()
        os.environ.pop("BUDGET_GATE_TOKENS", None)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return notes


async def main() -> None:
    _install_fake_provider()
    _install_tracer()
    notes: list[str] = []
    scenarios = [
        ("S1 文本主线", lambda: run_flow()),
        ("S2 带图PDF视觉链", lambda: run_flow(make_simple_pdf(with_image=True))),
        ("S3 HTTP 层", s3_http_layer),
        ("S4 HITL+预算", s4_hitl_budget),
    ]
    for name, fn in scenarios:
        print(f"\n######## 场景 {name} ########", flush=True)
        try:
            out = await fn()
            if out:
                notes.extend(out)
            print(f"[scenario] {name} OK", flush=True)
        except Exception as exc:  # noqa: BLE001 - probe still reports coverage
            import traceback
            print(f"[scenario] {name} FAILED: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            notes.append(f"{name} 失败: {type(exc).__name__}: {exc}")
    sys.settrace(None)
    threading.settrace(None)
    write_report("\n".join(notes) if notes else None)


if __name__ == "__main__":
    asyncio.run(main())
