import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from gitea_auto_reviewer.evidence import Evidence, collect_evidence, safe_evidence_environment


def test_evidence_environment_removes_runner_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "secret")
    monkeypatch.setenv("CODEX_HOME", "credential-store")
    monkeypatch.setenv("COMPANY_SECRET", "secret")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "configurations.ci")
    monkeypatch.setenv("NOX_MES_CI_LIVE_PATH", r"C:\devprojects_test\shared_test\live.py")

    environment = safe_evidence_environment(tmp_path)

    assert "GITEA_TOKEN" not in environment
    assert "CODEX_HOME" not in environment
    assert "COMPANY_SECRET" not in environment
    assert environment["DJANGO_SETTINGS_MODULE"] == "configurations.ci"
    assert environment["NOX_MES_CI_LIVE_PATH"] == r"C:\devprojects_test\shared_test\live.py"
    assert environment["USERPROFILE"] == str(tmp_path)


def test_collects_checks_for_exact_head(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("", encoding="utf-8")
    head = "a" * 40

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return CompletedProcess(command, 0, head + "\n", "")
        if "pytest" in command:
            return CompletedProcess(command, 0, "147 passed in 1.0s", "")
        return CompletedProcess(command, 0, "System check identified no issues", "")

    monkeypatch.setattr("gitea_auto_reviewer.evidence.subprocess.run", fake_run)

    evidence = collect_evidence(tmp_path, head)

    assert evidence.head_sha == head
    assert evidence.django_check.status == "pass"
    assert evidence.migration_check.status == "pass"
    assert (evidence.pytest.passed, evidence.pytest.total) == (147, 147)
    assert Evidence.from_json(evidence.to_json()) == evidence


def test_rejects_evidence_from_other_head(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gitea_auto_reviewer.evidence.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0, "b" * 40 + "\n", ""),
    )

    with pytest.raises(ValueError, match="does not match"):
        collect_evidence(tmp_path, "a" * 40)


def test_rejects_unknown_evidence_fields() -> None:
    with pytest.raises(ValueError):
        Evidence.from_json(json.dumps({"version": 1, "head_sha": "a" * 40, "checks": {}, "extra": 1}))


def test_migration_command_error_is_not_reported_as_missing(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("", encoding="utf-8")
    head = "a" * 40

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return CompletedProcess(command, 0, head + "\n", "")
        if "makemigrations" in command:
            return CompletedProcess(command, 1, "", "ModuleNotFoundError")
        return CompletedProcess(command, 0, "1 passed", "")

    monkeypatch.setattr("gitea_auto_reviewer.evidence.subprocess.run", fake_run)

    assert collect_evidence(tmp_path, head).migration_check.status == "error"
