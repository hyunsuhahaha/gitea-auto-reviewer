from gitea_auto_reviewer.cli import build_parser, main


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
