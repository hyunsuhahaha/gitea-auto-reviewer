import json

from gitea_auto_reviewer.cli import build_parser, main
from gitea_auto_reviewer.reproduction import ReproductionEvidence, ReproductionResult


def test_reasoning_defaults_and_environment_overrides(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AI_REVIEW_FIRST_PASS_EFFORT", raising=False)
    monkeypatch.delenv("AI_REVIEW_PLAN_EFFORT", raising=False)
    monkeypatch.delenv("AI_REVIEW_VERIFY_EFFORT", raising=False)
    assert main(["reasoning"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "first-pass=medium", "plan=medium", "verify=low",
    ]

    monkeypatch.setenv("AI_REVIEW_FIRST_PASS_EFFORT", "low")
    monkeypatch.setenv("AI_REVIEW_PLAN_EFFORT", "high")
    monkeypatch.setenv("AI_REVIEW_VERIFY_EFFORT", "medium")
    assert main(["reasoning"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "first-pass=low", "plan=high", "verify=medium",
    ]


def test_cli_reasoning_option_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("AI_REVIEW_FIRST_PASS_EFFORT", "low")
    arguments = build_parser().parse_args(["review", "--evidence-file", "evidence.json",
                                           "--reasoning-effort", "high"])
    assert arguments.reasoning_effort == "high"


def test_reproduce_command_retries_inconclusive_evidence(monkeypatch, tmp_path) -> None:
    sha = "a" * 40
    plan_file = tmp_path / "plan.json"
    output_file = tmp_path / "evidence.json"
    plan_file.write_text(json.dumps({
        "version": 1,
        "head_sha": sha,
        "cases": [{
            "finding_index": 0,
            "condition": "조건",
            "oracle": "기준",
            "script": "def reproduce():\n    return {}\n",
        }],
    }), encoding="utf-8")
    initial = ReproductionEvidence(sha, (
        ReproductionResult(0, "inconclusive", "조건", "기준", "", "fixture 오류", False, 0.1),
    ))
    retried = ReproductionEvidence(sha, (
        ReproductionResult(0, "confirmed", "조건", "기준", "정상", "오류", True, 0.2),
    ))
    monkeypatch.setattr("gitea_auto_reviewer.cli.run_reproductions", lambda *args: initial)
    monkeypatch.setattr(
        "gitea_auto_reviewer.cli.retry_inconclusive_reproductions", lambda *args: retried,
    )

    assert main([
        "reproduce", "--head-sha", sha, "--plan-file", str(plan_file),
        "--python", "python", "--repo-dir", str(tmp_path), "--output", str(output_file),
    ]) == 0
    assert ReproductionEvidence.from_json(output_file.read_text(encoding="utf-8")) == retried
