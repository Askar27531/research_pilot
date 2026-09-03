"""Submit a UTF-8 Chinese topic through the consolidated project endpoint."""

from fastapi.testclient import TestClient

from app.main import app


def main() -> None:
    payload = {
        "research_question": "调研 2024 至 2026 年的 RGB-LWIR 图像配准方法",
        "current_approach": "基于特征点的配准",
        "difficulties": ["跨模态特征差异"],
        "target_metrics": ["配准误差"],
        "advanced": {
            "year_from": 2024,
            "year_to": 2026,
            "max_papers": 5,
            "sources": ["openalex", "crossref", "arxiv"],
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/projects",
            json=payload,
            headers={"X-Request-ID": "chinese-utf8-live-smoke"},
        )
    print(response.status_code)
    print(response.json())
    response.raise_for_status()


if __name__ == "__main__":
    main()
