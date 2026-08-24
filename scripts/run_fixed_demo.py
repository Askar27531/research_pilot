"""Run the fixed, offline release demo and save a machine-readable report."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

DEMO_TESTS = [
    "tests/api/test_projects.py",
    "tests/integration/documents/test_pdf_parser.py",
    "tests/integration/evidence/test_evidence_pipeline.py",
    "tests/api/test_experiments.py",
    "tests/api/test_artifacts.py",
    "tests/integration/test_item_recovery.py",
]


def main() -> int:
    command = [sys.executable, "-m", "pytest", "-q", *DEMO_TESTS]
    started_at = datetime.now(UTC)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    report = {
        "name": "ResearchPilot fixed offline demo",
        "started_at": started_at.isoformat(),
        "duration_seconds": round((datetime.now(UTC) - started_at).total_seconds(), 3),
        "command": command,
        "scenarios": [
            "project persistence and research API",
            "PDF parse and figure workspace",
            "evidence integrity and cross-paper analysis",
            "experiment proposal and human approval",
            "versioned artifacts",
            "item-level failure recovery",
        ],
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "pytest_output": result.stdout.strip(),
    }
    output = Path("evals/snapshots/fixed_demo_result.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
