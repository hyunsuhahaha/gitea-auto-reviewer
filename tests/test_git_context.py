from pathlib import Path
from subprocess import CompletedProcess

from gitea_auto_reviewer.git_context import collect_context


def test_policy_is_read_from_base_commit(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["rev-parse", "HEAD"]:
            return CompletedProcess(command, 0, f"{'b' * 40}\n", "")
        if "--name-only" in command:
            return CompletedProcess(command, 0, "a.py\nb.py\n", "")
        if command[1] == "diff":
            return CompletedProcess(command, 0, "diff --git a/a.py b/a.py\n", "")
        return CompletedProcess(command, 0, "Review database risks.", "")

    monkeypatch.setattr("gitea_auto_reviewer.git_context.subprocess.run", fake_run)

    context = collect_context(tmp_path, "a" * 40, "b" * 40, max_diff_bytes=1000)

    assert context.policy == "Review database risks."
    assert context.changed_files == 2
    assert ["git", "show", f"{'a' * 40}:AI_REVIEW.md"] in calls
    assert ["git", "diff", "--no-ext-diff", "--unified=80", f"{'a' * 40}...{'b' * 40}", "--"] in calls


def test_missing_policy_uses_safe_default(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, **kwargs):
        if command[1:3] == ["rev-parse", "HEAD"]:
            return CompletedProcess(command, 0, f"{'b' * 40}\n", "")
        if "--name-only" in command:
            return CompletedProcess(command, 0, "a.py\n", "")
        if command[1] == "diff":
            return CompletedProcess(command, 0, "diff", "")
        return CompletedProcess(command, 128, "", "not found")

    monkeypatch.setattr("gitea_auto_reviewer.git_context.subprocess.run", fake_run)

    context = collect_context(tmp_path, "a" * 40, "b" * 40)

    assert "No project-specific policy" in context.policy


def test_repository_must_be_checked_out_at_pr_head(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gitea_auto_reviewer.git_context.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0, f"{'c' * 40}\n", ""),
    )

    try:
        collect_context(tmp_path, "a" * 40, "b" * 40)
    except ValueError as exc:
        assert "PR head SHA" in str(exc)
    else:
        raise AssertionError("mismatched checkout was accepted")
