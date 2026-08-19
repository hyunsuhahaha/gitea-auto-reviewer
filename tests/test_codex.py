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
                    "database_change": "not_detected",
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
