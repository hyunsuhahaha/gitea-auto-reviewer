import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from gitea_auto_reviewer.codex import run_codex_review


def test_codex_runs_read_only_without_gitea_credentials(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "changed_files": 1,
                    "changed_file_paths": ["settings.py"],
                        "database_change": "not_detected",
                        "database_change_details": [],
                    "django_check": "not_run",
                    "migration": "not_run",
                    "api_contract": "not_detected",
                    "external_integration": "not_detected",
                    "external_integration_reason": None,
                    "external_integration_evidence": [],
                    "tests": {"status": "not_run", "passed": None, "total": None},
                    "risk": "low",
                    "risk_confidence": "high",
                    "risk_evidence": [],
                    "key_changes": ["설정값 변경"],
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("gitea_auto_reviewer.codex.subprocess.run", fake_run)
    monkeypatch.setenv("GITEA_TOKEN", "must-not-leak")
    monkeypatch.setenv("COMPANY_SECRET", "must-not-leak")

    repository = tmp_path / "repository"
    repository.mkdir()
    review = run_codex_review("prompt", repository, codex_binary="codex", temp_root=tmp_path)

    assert review.risk == "low"
    assert "--sandbox" in captured["command"]
    assert captured["command"][captured["command"].index("--config") + 1] == 'model_reasoning_effort="high"'
    assert "read-only" in captured["command"]
    assert captured["command"][captured["command"].index("--cd") + 1] == str(repository)
    assert "--skip-git-repo-check" not in captured["command"]
    assert "--ephemeral" in captured["command"]
    assert "GITEA_TOKEN" not in captured["env"]
    assert "COMPANY_SECRET" not in captured["env"]


def test_codex_failure_includes_stderr(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gitea_auto_reviewer.codex.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 1, "", "invalid_json_schema"),
    )

    with pytest.raises(RuntimeError, match="invalid_json_schema"):
        run_codex_review("prompt", tmp_path, temp_root=tmp_path)


def test_codex_applies_deterministic_fields_before_validation(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        payload = {
            "changed_files": 99,
            "changed_file_paths": ["wrong.py"],
            "database_change": "not_detected",
            "database_change_details": [],
            "django_check": "pass",
            "migration": "no_missing",
            "api_contract": "not_detected",
            "external_integration": "not_detected",
            "external_integration_reason": None,
            "external_integration_evidence": [],
            "tests": {"status": "pass", "passed": None, "total": None},
            "risk": "low",
            "risk_confidence": "high",
            "risk_evidence": [],
            "key_changes": ["설정값 변경"],
            "findings": [],
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("gitea_auto_reviewer.codex.subprocess.run", fake_run)

    review = run_codex_review(
        "prompt",
        tmp_path,
        temp_root=tmp_path,
        fixed_fields={
            "changed_files": 1,
            "changed_file_paths": ["settings.py"],
            "django_check": "pass",
            "migration": "no_missing",
            "tests": {"status": "not_run", "passed": None, "total": None},
        },
    )

    assert review.changed_files == 1
    assert review.tests.status == "not_run"


def test_codex_uses_requested_reasoning_effort(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({
            "changed_files": 1,
            "changed_file_paths": ["settings.py"],
            "database_change": "not_detected",
            "database_change_details": [],
            "django_check": "not_run",
            "migration": "not_run",
            "api_contract": "not_detected",
            "external_integration": "not_detected",
            "external_integration_reason": None,
            "external_integration_evidence": [],
            "tests": {"status": "not_run", "passed": None, "total": None},
            "risk": "low",
            "risk_confidence": "high",
            "risk_evidence": [],
            "key_changes": ["설정값 변경"],
            "findings": [],
        }), encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("gitea_auto_reviewer.codex.subprocess.run", fake_run)

    run_codex_review("prompt", tmp_path, temp_root=tmp_path, reasoning_effort="low")

    assert captured["command"][captured["command"].index("--config") + 1] == 'model_reasoning_effort="low"'


def test_codex_rejects_unknown_reasoning_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        run_codex_review("prompt", tmp_path, temp_root=tmp_path, reasoning_effort="none")
