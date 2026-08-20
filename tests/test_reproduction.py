import json

import pytest

from gitea_auto_reviewer.reproduction import (
    PLAN_SCHEMA,
    VERIFICATION_SCHEMA,
    ReproductionEvidence,
    ReproductionCase,
    ReproductionPlan,
    ReproductionResult,
    VerificationDecision,
    _RUNNER_SOURCE,
    finalize_review,
    _complete_evidence,
    validate_script,
)
from test_review import impact_payload
from gitea_auto_reviewer.review import Review, render_markdown


SHA = "a" * 40


def test_reproduction_runner_imports_project_from_checkout_root() -> None:
    assert 'sys.path.insert(0, str(Path.cwd()))' in _RUNNER_SOURCE


def test_reproduction_requires_one_result_per_planned_case() -> None:
    plan = ReproductionPlan(SHA, (
        ReproductionCase(0, "condition", "oracle", "def reproduce():\n    return {}\n"),
    ))
    with pytest.raises(RuntimeError, match="result count"):
        _complete_evidence(plan, [])


@pytest.mark.parametrize("schema", [PLAN_SCHEMA, VERIFICATION_SCHEMA])
def test_structured_output_const_nodes_declare_a_type(schema) -> None:
    def visit(value):
        if isinstance(value, dict):
            if "const" in value:
                assert "type" in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def test_plan_is_sha_bound_and_rejects_process_execution() -> None:
    raw = json.dumps({"version": 1, "head_sha": SHA, "cases": [{
        "finding_index": 0,
        "condition": "existing record",
        "oracle": "request succeeds",
        "script": "def reproduce():\n    return {'confirmed': False, 'expected': 'ok', 'observed': 'ok', 'cleanup_checks': []}\n",
    }]})
    assert ReproductionPlan.from_json(raw, 1).head_sha == SHA
    with pytest.raises(ValueError, match="forbidden"):
        validate_script("import subprocess\ndef reproduce():\n    subprocess.run(['x'])\n")
    with pytest.raises(ValueError, match="top level"):
        validate_script("print('side effect')\ndef reproduce():\n    return {}\n")


def test_finalize_keeps_only_confirmed_cleanup_verified_findings() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "기존 행", "성공", "성공", "오류 응답", True, 1.2),
    ))
    final = finalize_review(review, evidence)
    rendered = render_markdown(final, 1, SHA, "테스트")
    assert not final.findings
    assert len(final.reproduced_findings) == 1
    assert "재현된 문제" in rendered
    assert "관찰 결과: 오류 응답" in rendered
    assert "롤백 검증: 통과" in rendered


def test_finalize_suppresses_refuted_or_unverified_findings() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "refuted", "조건", "정상", "정상", "정상", True, 0.1),
    ))
    final = finalize_review(review, evidence)
    assert final.risk == "low"
    assert final.findings == final.reproduced_findings == ()


def test_second_pass_can_only_accept_confirmed_reproductions() -> None:
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "조건", "정상", "정상", "오류", True, 0.1),
        ReproductionResult(1, "refuted", "조건", "정상", "정상", "정상", True, 0.1),
    ))
    decision = VerificationDecision.from_json(json.dumps({
        "version": 1, "head_sha": SHA, "accepted_finding_indices": [0],
    }), evidence)
    assert decision.accepted_finding_indices == (0,)

    with pytest.raises(ValueError, match="only confirmed"):
        VerificationDecision.from_json(json.dumps({
            "version": 1, "head_sha": SHA, "accepted_finding_indices": [1],
        }), evidence)
