import json
import sqlite3
import subprocess
import sys

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
    _population,
    _reproduction_environment,
    build_plan_prompt,
    finalize_review,
    plan_reproductions,
    run_reproductions,
    retry_inconclusive_reproductions,
    verify_reproductions,
    _complete_evidence,
    validate_script,
)
from test_review import impact_payload
from gitea_auto_reviewer.review import Review, render_markdown


SHA = "a" * 40


def test_reproduction_runner_imports_project_from_checkout_root() -> None:
    assert 'sys.path.insert(0, str(Path.cwd()))' in _RUNNER_SOURCE


def test_runner_requires_target_reach_and_decides_from_observed_values(tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    database = repository / "test.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table auth_user (id integer primary key, username text)")
    (repository / "settings.py").write_text(
        "SECRET_KEY = 'test'\n"
        "INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes']\n"
        f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.sqlite3', 'NAME': {str(database)!r}}}}}\n"
        "USE_TZ = True\n",
        encoding="utf-8",
    )
    (repository / "pytest.ini").write_text(
        "[pytest]\nDJANGO_SETTINGS_MODULE = settings\n", encoding="utf-8"
    )
    (repository / "target.py").write_text(
        "def changed_value():\n    return 1\n", encoding="utf-8"
    )
    script = (
        "from target import changed_value\n"
        "def reproduce():\n"
        "    observed = changed_value()\n"
        "    return {'confirmed': False, 'expected': 2, 'observed': observed, 'cleanup_checks': "
        "[{'model': 'auth.User', 'lookup': {'username': 'missing'}, 'exists': False}]}\n"
    )
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    reached = run_reproductions(
        ReproductionPlan(sha, (ReproductionCase(
            0, "조건", "2", script, ("target.py:2",),
        ),)),
        repository, sys.executable, 30,
    ).results[0]
    missed = run_reproductions(
        ReproductionPlan(sha, (ReproductionCase(
            0, "조건", "2", script, ("target.py:20",),
        ),)),
        repository, sys.executable, 30,
    ).results[0]

    assert reached.status == "confirmed"
    assert reached.target_reached is True
    assert reached.reached_targets == ("target.py:2",)
    assert reached.expected == "2" and reached.observed == "1"
    assert missed.status == "inconclusive"
    assert missed.target_reached is False
    assert "도달하지 못함" in missed.observed


def test_reproduction_uses_pytest_django_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nDJANGO_SETTINGS_MODULE = configurations.ci\n", encoding="utf-8"
    )

    environment = _reproduction_environment(tmp_path / "home", tmp_path)

    assert environment["DJANGO_SETTINGS_MODULE"] == "configurations.ci"


def test_reproduction_plan_requires_korean_user_visible_text() -> None:
    prompt = build_plan_prompt(Review.from_json(json.dumps(impact_payload())), SHA)
    assert "condition" in prompt and "expected" in prompt and "observed" in prompt
    assert "condition` and `oracle` in Korean" in prompt
    assert "exact JSON-compatible business values" in prompt
    assert "1-6 concise, unnumbered lines" in prompt
    assert "Do not present arbitrary fixture values" in prompt
    assert "only in `observed`" in prompt
    assert "timezone.now()" in prompt
    assert "naive datetime" in prompt


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


def test_plan_ignores_duplicate_and_out_of_range_finding_indexes() -> None:
    case = {
        "condition": "조건",
        "oracle": "기대 결과",
        "script": "def reproduce():\n    return {'confirmed': False, 'expected': '정상', 'observed': '정상', 'cleanup_checks': []}\n",
    }
    raw = json.dumps({"version": 1, "head_sha": SHA, "cases": [
        {"finding_index": 0, **case},
        {"finding_index": 0, **case},
        {"finding_index": 4, **case},
    ]})

    plan = ReproductionPlan.from_json(raw, finding_count=2)

    assert [item.finding_index for item in plan.cases] == [0]


def test_plan_accepts_every_candidate_finding() -> None:
    script = "def reproduce():\n    return {'confirmed': False, 'expected': '정상', 'observed': '정상', 'cleanup_checks': []}\n"
    raw = json.dumps({"version": 1, "head_sha": SHA, "cases": [
        {"finding_index": index, "condition": "조건", "oracle": "기준", "script": script}
        for index in range(5)
    ]})

    assert len(ReproductionPlan.from_json(raw, finding_count=5).cases) == 5


def test_plan_skips_codex_when_there_are_no_findings(monkeypatch, tmp_path) -> None:
    payload = impact_payload()
    payload["findings"] = []
    monkeypatch.setattr(
        "gitea_auto_reviewer.reproduction.run_codex_json",
        lambda *args, **kwargs: pytest.fail("Codex must not run without findings"),
    )

    plan = plan_reproductions(
        Review.from_json(json.dumps(payload)), SHA, tmp_path, "codex", "gitnexus",
    )

    assert plan == ReproductionPlan(SHA, ())


def test_plan_uses_medium_reasoning(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_codex(*args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"version": 1, "head_sha": SHA, "cases": [{
            "finding_index": 0,
            "condition": "조건",
            "oracle": "기준",
            "script": "def reproduce():\n    return {}\n",
        }]})

    monkeypatch.setattr("gitea_auto_reviewer.reproduction.run_codex_json", fake_codex)

    plan = plan_reproductions(
        Review.from_json(json.dumps(impact_payload())), SHA, tmp_path, "codex", "gitnexus",
    )

    assert captured["reasoning_effort"] == "medium"
    assert plan.cases[0].target_evidence == (
        "product/models.py:31", "product/models.py:44", "product/services.py:18",
    )
    assert ReproductionPlan.from_json(plan.to_json(), 1) == plan


def test_reproduction_script_allows_in_memory_string_replacement() -> None:
    validate_script(
        "def reproduce():\n"
        "    observed = '100 -> 50'.replace('->', '→')\n"
        "    return {'confirmed': False, 'expected': 'ok', 'observed': observed, 'cleanup_checks': []}\n"
    )


def test_inconclusive_reproduction_is_repaired_and_retried(monkeypatch, tmp_path) -> None:
    script = "def reproduce():\n    return {'confirmed': True}\n"
    plan = ReproductionPlan(SHA, (ReproductionCase(0, "조건", "기준", script),))
    initial = ReproductionEvidence(SHA, (
        ReproductionResult(0, "inconclusive", "조건", "기준", "", "naive datetime", False, 0.1),
    ))
    captured = {}

    def fake_codex(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"version": 1, "head_sha": SHA, "cases": [{
            "finding_index": 0, "condition": "약화된 조건", "oracle": "약화된 기준", "script": script,
        }]})

    def fake_reproduce(repaired, *args, **kwargs):
        captured["repaired"] = repaired
        return ReproductionEvidence(SHA, (
            ReproductionResult(0, "confirmed", "조건", "기준", "정상", "오류", True, 0.2,
                               target_reached=True, reached_targets=("product/models.py:1",)),
        ))

    monkeypatch.setattr("gitea_auto_reviewer.reproduction.run_codex_json", fake_codex)
    monkeypatch.setattr("gitea_auto_reviewer.reproduction.run_reproductions", fake_reproduce)

    result = retry_inconclusive_reproductions(
        plan, initial, tmp_path, "python", 180, (), "codex", "gitnexus",
    )

    assert result.results[0].status == "confirmed"
    assert "naive datetime" in captured["prompt"]
    assert "only retry" in captured["prompt"]
    assert captured["repaired"].cases[0].condition == "조건"
    assert captured["repaired"].cases[0].oracle == "기준"


def test_failed_repair_keeps_an_actionable_reason(monkeypatch, tmp_path) -> None:
    plan = ReproductionPlan(SHA, (
        ReproductionCase(0, "조건", "기준", "def reproduce():\n    pass\n"),
    ))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "inconclusive", "조건", "기준", "", "첫 실패", False, 0.1),
    ))
    monkeypatch.setattr(
        "gitea_auto_reviewer.reproduction.run_codex_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("repair unavailable")),
    )

    result = retry_inconclusive_reproductions(
        plan, evidence, tmp_path, "python", 180, (), "codex", "gitnexus",
    )

    assert result.results[0].status == "inconclusive"
    assert result.results[0].observed == "첫 실패; 자동 수정 실패: RuntimeError: repair unavailable"


def test_second_inconclusive_result_is_marked_as_retried(monkeypatch, tmp_path) -> None:
    script = "def reproduce():\n    return {}\n"
    plan = ReproductionPlan(SHA, (ReproductionCase(0, "조건", "기준", script),))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "inconclusive", "조건", "기준", "", "첫 실패", False, 0.1),
    ))
    monkeypatch.setattr(
        "gitea_auto_reviewer.reproduction.run_codex_json",
        lambda *args, **kwargs: json.dumps({"version": 1, "head_sha": SHA, "cases": [{
            "finding_index": 0, "condition": "조건", "oracle": "기준", "script": script,
        }]}),
    )
    monkeypatch.setattr(
        "gitea_auto_reviewer.reproduction.run_reproductions",
        lambda *args, **kwargs: ReproductionEvidence(SHA, (
            ReproductionResult(0, "inconclusive", "조건", "기준", "", "두 번째 실패", False, 0.2),
        )),
    )

    result = retry_inconclusive_reproductions(
        plan, evidence, tmp_path, "python", 180, (), "codex", "gitnexus",
    )

    assert result.results[0].observed == "자동 수정 1회 후에도 실패: 두 번째 실패"


def test_retry_is_skipped_when_every_case_has_a_verdict(monkeypatch, tmp_path) -> None:
    plan = ReproductionPlan(SHA, ())
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "refuted", "조건", "기준", "정상", "정상", True, 0.1),
    ))
    monkeypatch.setattr(
        "gitea_auto_reviewer.reproduction.run_codex_json",
        lambda *args, **kwargs: pytest.fail("Codex repair must not run"),
    )

    assert retry_inconclusive_reproductions(
        plan, evidence, tmp_path, "python", 180, (), "codex", "gitnexus",
    ) is evidence


def test_finalize_keeps_only_confirmed_cleanup_verified_findings() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "기존 행", "성공", "성공", "오류 응답", True, 1.2,
                           "포장 투입 버킷", 467, 2481, True, ("product/models.py:31",)),
    ))
    final = finalize_review(review, evidence)
    rendered = render_markdown(final, 1, SHA, "테스트")
    assert not final.findings
    assert len(final.reproduced_findings) == 1
    assert "재현된 문제" in rendered
    assert "관찰 결과: 오류 응답" in rendered
    assert "버그 조건 충족률: 포장 투입 버킷 467/2,481건 (18.82%)" in rendered
    assert "롤백 검증: 통과" in rendered


def test_invalid_population_is_ignored_without_changing_reproduction_status() -> None:
    assert _population({"population_label": "버킷", "matching_count": 2, "total_count": 1}) == (
        None, None, None,
    )


def test_finalize_retains_a_single_failed_reproduction_as_static_analysis() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "refuted", "조건", "정상", "정상", "정상", True, 0.1,
                           target_reached=True, reached_targets=("product/models.py:31",)),
    ))
    final = finalize_review(review, evidence)
    assert final.findings[0].reproduction_status == "not_reproduced"
    assert final.reproduced_findings == ()
    assert "재현 상태: 실행상 미재현 — 정상" in render_markdown(final, 1, SHA, "테스트")


def test_finalize_retains_unplanned_or_inconclusive_findings_as_static_analysis() -> None:
    review = Review.from_json(json.dumps(impact_payload()))

    unplanned = finalize_review(review, ReproductionEvidence(SHA, ()))
    inconclusive = finalize_review(review, ReproductionEvidence(SHA, (
        ReproductionResult(0, "inconclusive", "조건", "정상", "", "시간 초과", False, 180.0),
    )))

    assert unplanned.findings[0].reproduction_status == "unplanned"
    assert inconclusive.findings[0].reproduction_status == "inconclusive"
    assert inconclusive.findings[0].reproduction_detail == "시간 초과"
    rendered = render_markdown(Review.from_json(inconclusive.to_json()), 1, SHA, "테스트")
    assert "재현 상태: 실행 불확정 — 시간 초과" in rendered
    assert unplanned.risk == inconclusive.risk == review.risk


def test_finalize_retains_second_pass_rejection_as_static_analysis() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "조건", "정상", "정상", "오류", True, 0.1,
                           target_reached=True, reached_targets=("product/models.py:31",)),
    ))

    final = finalize_review(review, evidence, VerificationDecision(SHA, ()))

    assert final.findings[0].reproduction_status == "verification_rejected"
    assert final.reproduced_findings == ()


def test_confirmed_claim_without_target_reach_is_not_publishable() -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "조건", "정상", "1", "2", True, 0.1),
    ))

    final = finalize_review(review, evidence)

    assert final.reproduced_findings == ()
    assert final.findings[0].reproduction_status == "inconclusive"
    with pytest.raises(ValueError, match="only confirmed"):
        VerificationDecision.from_json(json.dumps({
            "version": 1, "head_sha": SHA, "accepted_finding_indices": [0],
        }), evidence)


def test_second_pass_inspects_the_executed_script(monkeypatch, tmp_path) -> None:
    review = Review.from_json(json.dumps(impact_payload()))
    script = "def reproduce():\n    return {'expected': 1, 'observed': 2}\n"
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(
            0, "confirmed", "조건", "정상", "1", "2", True, 0.1,
            target_reached=True, reached_targets=("product/models.py:31",), script=script,
        ),
    ))
    captured = {}

    def fake_codex(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "version": 1, "head_sha": SHA, "accepted_finding_indices": [0],
        })

    monkeypatch.setattr("gitea_auto_reviewer.reproduction.run_codex_json", fake_codex)

    decision = verify_reproductions(review, evidence, tmp_path, "codex", "gitnexus")

    assert decision.accepted_finding_indices == (0,)
    assert json.dumps(script, ensure_ascii=False)[1:-1] in captured["prompt"]
    assert "not derived from the reached target" in captured["prompt"]


def test_second_pass_can_only_accept_confirmed_reproductions() -> None:
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "조건", "정상", "정상", "오류", True, 0.1,
                           target_reached=True, reached_targets=("product/models.py:31",)),
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
