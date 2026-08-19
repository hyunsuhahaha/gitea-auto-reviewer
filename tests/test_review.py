import json

import pytest

from gitea_auto_reviewer.review import Review, render_markdown, validate_grounding, validate_verification


def impact_payload() -> dict[str, object]:
    return {
        "changed_files": 7,
        "database_change": "yes",
        "django_check": "pass",
        "migration": "no_missing",
        "api_contract": "changed",
        "external_integration": "possible",
        "external_integration_reason": "SCM 상품 동기화 경로에서 Product 생성 확인",
        "external_integration_evidence": ["scm/product_sync.py:74"],
        "tests": {"status": "pass", "passed": 147, "total": 147},
        "risk": "high",
        "risk_confidence": "high",
        "risk_evidence": ["product/models.py:31"],
        "key_changes": ["Product.remark 추가", "상품 등록 API 변경"],
        "findings": [{
            "category": "bug",
            "problem": "기존 Product 생성 경로가 remark를 전달하지 않음",
            "impact": "상품 생성이 실패할 수 있음",
            "change": "remark를 전달하거나 기본값을 정의",
            "expected_state": "기존 생성 경로가 정상 동작",
            "evidence": ["product/models.py:31", "product/services.py:18"],
            "policy_quote": None,
        }],
    }


def test_review_renders_compact_change_impact_summary() -> None:
    review = Review.from_json(json.dumps(impact_payload()))

    rendered = render_markdown(review, 214, "a" * 40, "상품 비고 기능 추가")

    assert "│ PR #214 상품 비고 기능 추가" in rendered
    assert "변경 파일" in rendered and "7개" in rendered
    assert "DB 변경" in rendered and "있음" in rendered
    assert "└ SCM 상품 동기화 경로에서 Product 생성 확인 — scm/product_sync.py:74" in rendered
    assert "검증된 사실" in rendered and "Django check PASS" in rendered
    assert "Migration check 누락 없음" in rendered
    assert "테스트" in rendered and "147/147 PASS" in rendered
    assert "위험도" in rendered and "🟠 HIGH · 근거 HIGH" in rendered
    assert "주의" in rendered
    assert "수정: remark를 전달하거나 기본값을 정의" in rendered
    assert "완료: 기존 생성 경로가 정상 동작" in rendered
    assert "└ product/models.py:31" in rendered
    assert "유용했다면 👍, 노이즈였다면 👎" in rendered
    assert "Critical" not in rendered


def test_not_run_is_explicit() -> None:
    payload = impact_payload()
    payload["tests"] = {"status": "not_run", "passed": None, "total": None}

    rendered = render_markdown(Review.from_json(json.dumps(payload)), 1, "b" * 40, "변경")

    assert "테스트" in rendered and "NOT RUN" in rendered


def test_empty_findings_are_silent() -> None:
    payload = impact_payload()
    payload.update(risk="low", risk_confidence="high", risk_evidence=[])
    payload["findings"] = []

    rendered = render_markdown(Review.from_json(json.dumps(payload)), 1, "b" * 40, "변경")

    assert "주의\n" not in rendered


def test_external_not_detected_has_no_abstract_reason() -> None:
    payload = impact_payload()
    payload.update(
        external_integration="not_detected",
        external_integration_reason=None,
        external_integration_evidence=[],
    )

    rendered = render_markdown(Review.from_json(json.dumps(payload)), 1, "b" * 40, "변경")

    assert "외부연동" in rendered and "직접 영향 미발견" in rendered
    assert "SCM 상품 동기화" not in rendered


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(risk="critical"),
        lambda value: value.update(key_changes=[]),
        lambda value: value.update(tests={"status": "pass", "passed": None, "total": None}),
        lambda value: value.update(changed_files=0),
        lambda value: value.update(database_change="no"),
        lambda value: value.update(external_integration_reason=None),
        lambda value: value.update(risk_evidence=[]),
        lambda value: value.update(findings=[{"category": "style"}]),
    ],
)
def test_review_rejects_invalid_output(mutation) -> None:
    payload = impact_payload()
    mutation(payload)

    with pytest.raises(ValueError):
        Review.from_json(json.dumps(payload))


def test_grounding_checks_files_lines_and_policy_quotes(tmp_path) -> None:
    source = tmp_path / "product" / "models.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\n", encoding="utf-8")
    payload = impact_payload()
    payload["risk_evidence"] = ["product/models.py:2"]
    payload.update(
        external_integration="not_detected",
        external_integration_reason=None,
        external_integration_evidence=[],
    )
    payload["findings"] = [{
        "category": "policy",
        "problem": "트랜잭션 규칙 위반",
        "impact": "부분 저장 가능",
        "change": "atomic 적용",
        "expected_state": "전체 작업이 원자적으로 저장됨",
        "evidence": ["product/models.py:2"],
        "policy_quote": "쓰기 작업은 atomic이어야 한다.",
    }]

    review = Review.from_json(json.dumps(payload))
    validate_grounding(review, tmp_path, "쓰기 작업은 atomic이어야 한다.")

    with pytest.raises(ValueError, match="quote"):
        validate_grounding(review, tmp_path, "다른 정책")

    payload["risk_evidence"] = ["product/models.py:3"]
    with pytest.raises(ValueError, match="line does not exist"):
        validate_grounding(Review.from_json(json.dumps(payload)), tmp_path, "쓰기 작업은 atomic이어야 한다.")


def test_verifier_may_only_remove_verbatim_findings() -> None:
    draft = Review.from_json(json.dumps(impact_payload()))
    empty = impact_payload()
    empty["findings"] = []
    validate_verification(draft, Review.from_json(json.dumps(empty)))

    rewritten = impact_payload()
    rewritten["findings"][0]["problem"] = "검증기가 새로 쓴 주장"
    with pytest.raises(ValueError, match="introduced or rewrote"):
        validate_verification(draft, Review.from_json(json.dumps(rewritten)))

    downgraded = impact_payload()
    downgraded.update(
        external_integration="not_detected",
        external_integration_reason=None,
        external_integration_evidence=[],
    )
    validate_verification(draft, Review.from_json(json.dumps(downgraded)))
