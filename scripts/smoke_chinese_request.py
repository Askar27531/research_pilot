from fastapi.testclient import TestClient

from app.main import app


def main() -> None:
    payload = {
        "research_question": "调研2024至2026年的RGB-LWIR图像配准方法",
        "keywords": ["RGB-LWIR", "图像配准"],
        "year_from": 2024,
        "year_to": 2026,
        "maximum_papers": 5,
    }
    with TestClient(app) as client:
        response = client.post(
            "/research/test",
            json=payload,
            headers={"X-Request-ID": "chinese-utf8-live-smoke"},
        )
    print(response.status_code)
    print(response.json())
    response.raise_for_status()


if __name__ == "__main__":
    main()
