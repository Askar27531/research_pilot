"""Offline decision-desk QA walkthrough (no Ollama, no network).

Builds a throwaway project in a temp database (real migrations incl. V13),
seeds a report + evidence + mixed reviews (auto confirmed / auto doubt /
human excluded), then exercises the real service layer end to end:

- WorkspaceService.review_desk(project_id, "priority" | "all")
- WorkspaceService.review_preview(...) impact echo for "excluded"
- ResearchDataRepository.review_evidence(source="human") commit path

Print output is meant to be read by a human as a manual acceptance pass.

Run:  .venv/Scripts/python.exe scripts/qa_review_desk.py
"""

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.api.tokens import issue_token
from app.db import Database, ProjectRepository, ResearchDataRepository
from app.db.research_session_repository import ResearchSessionRepository
from app.schemas import (
    AnalysisClaim,
    AnalysisReport,
    EvidenceNode,
    PaperAnalysis,
    ResearchRequest,
)
from app.schemas.review import ReviewPreviewRequest
from app.services import WorkspaceService

SHA = "0" * 64


def _node(evidence_id: str, evidence_type: str, confidence: float,
          excerpt: str | None = None) -> EvidenceNode:
    common = {
        "evidence_id": evidence_id, "project_id": "__pid__", "paper_id": "paper-a",
        "document_id": "doc-a", "evidence_type": evidence_type,
        "claim": f"证据 {evidence_id} 的观察结论", "confidence": confidence,
        "page_number": 3, "label": f"Figure-{evidence_id[-1]}",
        "source_path": "fig.png", "source_hash": SHA,
        "created_at": "2026-01-01T00:00:00Z",
    }
    if evidence_type == "text":
        common.update(label=None, claim="全文证据块（第 1 页）", excerpt=excerpt,
                      span_start=0, span_end=len(excerpt or "x"))
    return EvidenceNode(**common)


async def _seed(database: Database, project_id: str, paper_id: str) -> None:
    """Seed one report + four evidence rows (three visual, one text)."""
    def claim(value: str, *ids: str) -> AnalysisClaim:
        return AnalysisClaim(value=value, kind="supported", evidence_ids=list(ids))
    paper = PaperAnalysis(
        paper_id=paper_id, title="Demo Paper",
        core_problem=[claim("Demo Paper 的核心结论", "ev1", "ev2")],
        methods=[claim("Demo Paper 的方法支撑点", "ev2")],
        mechanisms=[], experimental_setup=[], main_results=[], limitations=[],
        relevance_to_topic=[], overview=None,
    )
    report = AnalysisReport(
        project_id=project_id, search_revision=1, papers=[paper],
        comparison=None, created_at="2026-01-02T00:00:00Z",
    )
    await ResearchSessionRepository(database).save_report(report)

    nodes = {
        "ev1": _node("ev1", "figure", 0.7),
        "ev2": _node("ev2", "figure", 0.9),
        "ev3": _node("ev3", "figure", 0.8),
        "txt1": _node("txt1", "text", 1.0, excerpt="未被任何结论引用的正文段落。"),
    }
    async with database.connect() as connection:
        await connection.execute("PRAGMA foreign_keys=OFF")
        await connection.executemany(
            "INSERT INTO evidence(id,project_id,paper_id,document_id,evidence_type,"
            "locator_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [
                (node.evidence_id, project_id, paper_id, node.document_id,
                 node.evidence_type, f"loc:{node.evidence_id}",
                 node.model_copy(update={"project_id": project_id}).model_dump_json(),
                 node.created_at)
                for node in nodes.values()
            ],
        )
        await connection.commit()


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rp-qa-desk-"))
    try:
        database = Database(root / "qa.db")
        await database.initialize()
        project = await ProjectRepository(database).create(
            "QA-决策台", ResearchRequest(research_question="决策台离线冒烟")
        )
        project_id = project.id
        await ResearchSessionRepository(database).begin_search(project_id)
        await _seed(database, project_id, "paper-a")

        research = ResearchDataRepository(database)
        # Auto passes wrote these before the human ever opened the desk:
        await research.review_evidence(
            project_id, "ev1", "doubted", "自动复核：图与正文结论存在矛盾",
            source="auto_visual_verifier",
        )
        await research.review_evidence(
            project_id, "ev2", "confirmed", "图文一致：确认支持结论",
            source="auto_consistency",
        )
        await research.review_evidence(
            project_id, "ev3", "excluded", "人工排除", source="human",
        )

        service = WorkspaceService(
            SimpleNamespace(state=SimpleNamespace(
                database=database, document_service=None,
            )),
            database,
        )

        print("=" * 70)
        print("1) 优先队列 review_desk(priority)")
        print("=" * 70)
        desk = await service.review_desk(project_id, "priority")
        print("stats:", dict(desk.stats))
        for item in desk.items:
            print(
                f"- [{item.type} {item.label}] 页{item.page} 置信度{item.confidence:.0%} "
                f"review={item.review_status}/{item.review_source} "
                f"cited_by={item.cited_by} priority={item.priority:.2f}"
            )
            print(f"    引用结论: {[c.value for c in item.citing]}")
            print(f"    机器判定: {item.review_note}")

        print()
        print("=" * 70)
        print("2) 排除影响回显 review_preview(excluded) 对 ev1")
        print("=" * 70)
        token = issue_token(project_id, "evidence", "ev1")
        preview = await service.review_preview(
            project_id, ReviewPreviewRequest(evidence_token=token, status="excluded")
        )
        print("cited_total:", preview.cited_total)
        print("downgrade  :", [c.value for c in preview.downgrade])
        print("retained   :", [c.value for c in preview.retained])
        print("message    :", preview.message)

        print()
        print("=" * 70)
        print("3) 人工提交排除 ev1（source=human）后队列变化")
        print("=" * 70)
        await research.review_evidence(
            project_id, "ev1", "excluded", "人工复核后确认排除", source="human",
        )
        after = await service.review_desk(project_id, "all")
        print("stats:", dict(after.stats))
        print("可操作条目:", [item.evidence_id for item in after.items])
        ev1_gone = all(item.evidence_id != "ev1" for item in after.items)
        print("ev1 已从队列消失:", ev1_gone)
        assert after.stats.excluded == 2  # ev3(seed) + ev1(committed)
        assert ev1_gone
        print()
        print("OK: offline decision-desk smoke passed (migration, queue, "
              "impact echo and human commit all behave as expected)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
