import json

import pytest

from gitea_auto_reviewer.review import (
    REVIEW_JSON_SCHEMA,
    Review,
    render_markdown,
    validate_grounding,
    validate_verification,
)


def impact_payload() -> dict[str, object]:
    return {
        "changed_files": 7,
        "changed_file_paths": [
            "product/models.py",
            "product/services.py",
            "product/api.py",
            "product/serializers.py",
            "scm/product_sync.py",
            "tests/test_product.py",
            "product/migrations/0008_product_remark.py",
        ],
        "database_change": "yes",
        "database_change_details": [{
            "description": "product_product 테이블에 remark 컬럼 추가",
            "evidence": ["product/migrations/0008_product_remark.py:18"],
        }],
        "data_change": "changed",
        "data_change_details": [{
            "description": "Product.remark 저장값을 상품 조회 응답에 포함",
            "evidence": ["product/services.py:18"],
        }],
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
            "evidence": ["product/models.py:31", "product/models.py:44", "product/services.py:18"],
            "policy_quote": None,
        }],
        "reproduced_findings": [],
        "affected_files": [{
            "path": "app/views/product_admin.py",
            "reason": "Product 생성 호출자가 새 필드의 영향을 받음",
            "evidence": ["app/views/product_admin.py:52"],
        }],
    }


def test_codex_schema_does_not_mix_ref_with_other_keywords() -> None:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert set(value) == {"$ref"}
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(REVIEW_JSON_SCHEMA)


def test_codex_schema_const_nodes_declare_a_type() -> None:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            if "const" in value:
                assert "type" in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(REVIEW_JSON_SCHEMA)


def test_review_renders_compact_change_impact_summary() -> None:
    payload = impact_payload()
    finding = payload["findings"][0]
    payload["reproduced_findings"] = [{
        "problem": finding["problem"], "impact": finding["impact"], "evidence": finding["evidence"],
        "condition": "remark 없이 기존 생성 경로 호출", "oracle": "기존 생성 요청이 성공해야 함",
        "expected": "생성 성공", "observed": "필수 필드 오류 응답", "cleanup_verified": True,
    }]
    review = Review.from_json(json.dumps(payload))

    rendered = render_markdown(review, 214, "a" * 40, "상품 비고 기능 추가")

    assert "│ PR #214 상품 비고 기능 추가" in rendered
    assert "변경 파일" in rendered and "7개" in rendered
    assert "└ product/models.py" in rendered
    assert "DB 스키마 변경" in rendered and "있음" in rendered
    assert "product_product 테이블에 remark 컬럼 추가 — product/migrations/0008_product_remark.py:18" in rendered
    assert "데이터 처리 변경" in rendered and "Product.remark 저장값" in rendered
    assert "└ SCM 상품 동기화 경로에서 Product 생성 확인 — scm/product_sync.py:74" in rendered
    assert "검증된 사실" in rendered and "Django check PASS" in rendered
    assert "Migration check 누락 없음" in rendered
    assert "테스트" in rendered and "147/147 PASS" in rendered
    assert "위험도" in rendered and "🟠 HIGH · 근거 HIGH" in rendered
    assert "재현된 문제" in rendered
    assert "수정:" not in rendered
    assert "완료:" not in rendered
    assert "└ product/models.py:31" in rendered
    assert "유용했다면" not in rendered
    assert "노이즈였다면" not in rendered
    assert "Critical" not in rendered
    warning = rendered[rendered.index("재현된 문제"):rendered.index("영향 파일")]
    assert warning.count("└ product/models.py") == 1
    assert "product/models.py:31" not in warning
    assert "product/models.py:44" not in warning
    assert "└ product/services.py" in warning
    assert rendered.rfind("영향 파일") > rendered.rfind("재현된 문제")
    assert "• app/views/product_admin.py" in rendered
    assert "Product 생성 호출자가 새 필드의 영향을 받음 — app/views/product_admin.py:52" in rendered


def test_not_run_is_explicit() -> None:
    payload = impact_payload()
    payload["affected_files"] = []
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


def test_database_not_detected_has_no_details() -> None:
    payload = impact_payload()
    payload.update(database_change="not_detected", database_change_details=[])

    rendered = render_markdown(Review.from_json(json.dumps(payload)), 1, "b" * 40, "변경")

    assert "DB 스키마 변경" in rendered and "직접 영향 미발견" in rendered
    assert "product_product 테이블" not in rendered


def test_key_changes_accepts_ten_items_but_rejects_eleven() -> None:
    payload = impact_payload()
    payload["key_changes"] = [f"변경 {index}" for index in range(10)]
    review = Review.from_json(json.dumps(payload))
    assert len(review.key_changes) == 10

    payload["key_changes"].append("그 외 관련 변경 1건")
    with pytest.raises(ValueError, match="1-10"):
        Review.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(risk="critical"),
        lambda value: value.update(key_changes=[]),
        lambda value: value.update(tests={"status": "pass", "passed": None, "total": None}),
        lambda value: value.update(changed_files=0),
        lambda value: value.update(database_change="no"),
        lambda value: value.update(database_change_details=[]),
        lambda value: value.update(data_change_details=[]),
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
    payload["affected_files"] = []
    payload["risk_evidence"] = ["product/models.py:2"]
    payload.update(database_change="not_detected", database_change_details=[], data_change="not_detected", data_change_details=[])
    payload.update(
        external_integration="not_detected",
        external_integration_reason=None,
        external_integration_evidence=[],
    )
    payload["findings"] = [{
        "category": "policy",
        "problem": "트랜잭션 규칙 위반",
        "impact": "부분 저장 가능",
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

    database_downgraded = impact_payload()
    database_downgraded.update(database_change="not_detected", database_change_details=[])
    validate_verification(draft, Review.from_json(json.dumps(database_downgraded)))

    data_downgraded = impact_payload()
    data_downgraded.update(data_change="not_detected", data_change_details=[])
    validate_verification(draft, Review.from_json(json.dumps(data_downgraded)))

    affected_removed = impact_payload()
    affected_removed["affected_files"] = []
    validate_verification(draft, Review.from_json(json.dumps(affected_removed)))

    affected_rewritten = impact_payload()
    affected_rewritten["affected_files"][0]["reason"] = "검증기가 새로 쓴 영향"
    with pytest.raises(ValueError, match="affected file"):
        validate_verification(draft, Review.from_json(json.dumps(affected_rewritten)))


def test_renderer_states_when_no_reproduced_problem_survives() -> None:
    payload = impact_payload()
    payload["findings"] = []
    review = Review.from_json(json.dumps(payload))

    rendered = render_markdown(review, 1, "a" * 40, "테스트")

    assert "재현된 문제\n• 자동 재현 및 2차 검증을 통과" in rendered
