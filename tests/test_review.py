import json

import pytest

from gitea_auto_reviewer.review import Review, render_markdown


def impact_payload() -> dict[str, object]:
    return {
        "changed_files": 7,
        "database_change": "yes",
        "django_check": "pass",
        "migration": "no_missing",
        "api_contract": "changed",
        "external_integration": "possible",
        "tests": {"status": "pass", "passed": 147, "total": 147},
        "risk": "high",
        "key_changes": ["Product.remark 추가", "상품 등록 API 변경"],
        "new_assumptions": ["모든 Product 생성 경로가 remark를 처리해야 함"],
        "cautions": ["SCM 상품 동기화 코드에서 Product 생성 경로 발견"],
        "human_checks": ["기존 상품 생성", "migration rollback"],
    }


def test_review_renders_compact_change_impact_summary() -> None:
    review = Review.from_json(json.dumps(impact_payload()))

    rendered = render_markdown(review, 214, "a" * 40, "상품 비고 기능 추가")

    assert "│ PR #214 상품 비고 기능 추가" in rendered
    assert "변경 파일" in rendered and "7개" in rendered
    assert "DB 변경" in rendered and "있음" in rendered
    assert "검증된 사실" in rendered and "Django check PASS" in rendered
    assert "Migration check 누락 없음" in rendered
    assert "테스트" in rendered and "147/147 PASS" in rendered
    assert "위험도" in rendered and "🟠 HIGH" in rendered
    assert "새롭게 생긴 가정" in rendered
    assert "□ migration rollback" in rendered
    assert "Critical" not in rendered


def test_not_run_is_explicit() -> None:
    payload = impact_payload()
    payload["tests"] = {"status": "not_run", "passed": None, "total": None}

    rendered = render_markdown(Review.from_json(json.dumps(payload)), 1, "b" * 40, "변경")

    assert "테스트" in rendered and "NOT RUN" in rendered


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(risk="critical"),
        lambda value: value.update(key_changes=[]),
        lambda value: value.update(tests={"status": "pass", "passed": None, "total": None}),
        lambda value: value.update(changed_files=0),
    ],
)
def test_review_rejects_invalid_output(mutation) -> None:
    payload = impact_payload()
    mutation(payload)

    with pytest.raises(ValueError):
        Review.from_json(json.dumps(payload))
