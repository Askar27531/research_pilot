"""Run the paper-assistant workflow through the consolidated REST API."""

import json
import os
import sys
import time
from pathlib import Path

import httpx

API = os.getenv("RESEARCHPILOT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def checked(response: httpx.Response) -> dict:
    if response.is_error:
        try:
            detail = json.dumps(response.json(), ensure_ascii=False, indent=2)
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"HTTP {response.status_code} for {response.request.method} "
            f"{response.request.url}:\n{detail}"
        )
    return response.json() if response.content else {}


def main() -> int:
    demo_dir = Path(os.getenv("RESEARCHPILOT_DEMO_PDF_DIR", "data/demo_papers"))
    with httpx.Client(base_url=API, timeout=None) as client:
        health = checked(client.get("/health"))
        if not health["paper_analysis"]["ready"]:
            raise RuntimeError(
                "Paper analysis is not ready: "
                + "; ".join(health["paper_analysis"]["blockers"])
            )
        result = checked(client.post("/projects", json={
            "research_question": "如何用多智能体强化学习协调多无人机执行森林火灾扑救？",
            "current_approach": "MAPPO",
            "difficulties": ["部分可观测", "动态任务分配", "安全约束"],
            "target_metrics": ["过火面积", "控制时间", "资源消耗", "任务完成率"],
            "advanced": {
                "max_papers": 6,
                "sources": ["openalex", "crossref", "arxiv"],
            },
        }))
        workspace = result["workspace"]
        project_id = workspace["project_id"]
        deadline = time.monotonic() + 1_800
        uploaded = False
        while time.monotonic() < deadline:
            workspace = checked(client.get(f"/projects/{project_id}/workspace"))
            action = workspace["next_action"]
            if workspace["user_stage"] == "paper_selection":
                tokens = [paper["paper_token"] for paper in workspace["literature"][:2]]
                if not tokens:
                    raise RuntimeError("Search completed without selectable papers")
                checked(client.post(
                    f"/projects/{project_id}/actions",
                    json={
                        "type": "select_papers",
                        "paper_tokens": tokens,
                        "analysis_requirements": "比较方法机制、评价指标与安全约束",
                    },
                ))
                continue
            if workspace["user_stage"] == "analysis_review":
                print(json.dumps({
                    "stage": workspace["user_stage"],
                    "papers": len(workspace["literature"]),
                    "analyzed_papers": len((workspace.get("analysis_report") or {}).get("papers", [])),
                    "uploaded_documents": uploaded,
                }, ensure_ascii=False, indent=2))
                return 0
            if action["type"] == "wait":
                time.sleep(2)
                continue
            if action["type"] == "upload_documents":
                pdfs = iter(sorted(demo_dir.glob("*.pdf"))) if demo_dir.is_dir() else iter(())
                for item in action["documents"]:
                    pdf = next(pdfs, None)
                    if pdf is None:
                        print(json.dumps(workspace, ensure_ascii=False, indent=2))
                        print(f"缺少《{item['title']}》全文；请把 PDF 放入 {demo_dir}。", file=sys.stderr)
                        return 2
                    with pdf.open("rb") as stream:
                        checked(client.post(
                            f"/projects/{project_id}/documents",
                            data={"upload_token": item["upload_token"]},
                            files=[("files", (pdf.name, stream, "application/pdf"))],
                        ))
                uploaded = True
                continue
            if action["type"] == "retry":
                checked(client.post(
                    f"/projects/{project_id}/actions", json={"type": "run"}
                ))
                continue
        raise TimeoutError("The demo did not complete within 30 minutes")


if __name__ == "__main__":
    raise SystemExit(main())
