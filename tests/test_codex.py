import json
from pathlib import Path
from subprocess import CompletedProcess

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
                    "database_change": "no",
                    "django_check": "not_run",
                    "migration": "not_run",
                    "api_contract": "unchanged",
                    "external_integration": "unchanged",
                    "tests": {"status": "not_run", "passed": None, "total": None},
                    "risk": "low",
                    "key_changes": ["설정값 변경"],
                    "new_assumptions": ["새 설정값을 사용하는 경로가 동일하게 동작해야 함"],
                    "cautions": [],
                    "human_checks": ["기존 동작 확인"],
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
