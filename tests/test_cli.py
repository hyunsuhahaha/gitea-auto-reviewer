import json

import pytest

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
        ReproductionResult(0, "confirmed", "조건", "기준", "정상", "오류", True, 0.2,
                           target_reached=True, reached_targets=("product/models.py:1",)),
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


class FakeGiteaClient:
    instances: list["FakeGiteaClient"] = []

    def __init__(self, base_url, repository, token, head_sha="a" * 40, previous=None) -> None:
        self.head_sha = head_sha
        self.previous = previous
        self.posted: list[str] = []
        FakeGiteaClient.instances.append(self)

    def get_pull_request(self, pr_number):
        from gitea_auto_reviewer.gitea import PullRequestMetadata
        return PullRequestMetadata(pr_number, "제목", "b" * 40, self.head_sha, "owner/repo")

    def get_review_comment(self, pr_number, author=None):
        return self.previous

    def get_authenticated_login(self):
        return "ai-review-bot"

    def upsert_comment(self, pr_number, body, author=None):
        self.posted.append(body)
        return "created"


def _review_file(tmp_path):
    from test_review import impact_payload
    path = tmp_path / "review.json"
    path.write_text(json.dumps(impact_payload(), ensure_ascii=False), encoding="utf-8")
    return path


def _comment_args(review_file, sha="a" * 40):
    return ["comment", "--gitea-url", "https://gitea.example", "--repository", "owner/repo",
            "--pr", "7", "--pr-title", "제목", "--head-sha", sha, "--review-file", str(review_file)]


def test_comment_skips_stale_head(monkeypatch, tmp_path, capsys) -> None:
    FakeGiteaClient.instances.clear()
    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient",
                        lambda *args: FakeGiteaClient(*args, head_sha="c" * 40))

    assert main(_comment_args(_review_file(tmp_path))) == 0

    assert FakeGiteaClient.instances[0].posted == []
    assert "skipped stale review" in capsys.readouterr().out


def test_comment_posts_for_current_head(monkeypatch, tmp_path) -> None:
    FakeGiteaClient.instances.clear()
    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", FakeGiteaClient)

    assert main(_comment_args(_review_file(tmp_path))) == 0

    assert FakeGiteaClient.instances[0].posted[0].startswith(
        f"<!-- gitea-auto-reviewer:pr=7:sha={'a' * 40} -->"
    )


def test_comment_ignores_previous_state_when_bot_login_is_unknown(monkeypatch, tmp_path) -> None:
    FakeGiteaClient.instances.clear()
    calls = []

    class NoLoginClient(FakeGiteaClient):
        def get_authenticated_login(self):
            return None

        def get_review_comment(self, pr_number, author=None):
            calls.append(author)
            return super().get_review_comment(pr_number, author)

    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", NoLoginClient)

    assert main(_comment_args(_review_file(tmp_path))) == 0
    assert calls == []


def _metadata_args(tmp_path, *extra):
    return ["metadata", "--gitea-url", "https://gitea.example", "--repository", "owner/repo",
            "--pr", "7", "--output-file", str(tmp_path / "out.txt"), *extra]


def _metadata_outputs(tmp_path):
    lines = (tmp_path / "out.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.parametrize(("title", "draft", "labels", "files", "reason"), [
    ("기능", False, (), ("app/views.py",), ""),
    ("기능", False, ("skip-ai-review",), ("app/views.py",), "skip-ai-review label"),
    ("기능", True, (), ("app/views.py",), "draft pull request"),
    ("WIP: 기능", False, (), ("app/views.py",), "draft pull request"),
    ("문서", False, (), ("README.md", "docs/guide/setup.rst"), "documentation-only change"),
    ("의존성", False, (), ("README.md", "requirements.txt"), ""),
    ("거대", False, (), None, ""),
])
def test_metadata_reports_skip_reason(monkeypatch, tmp_path, title, draft, labels, files, reason) -> None:
    from gitea_auto_reviewer.gitea import PullRequestMetadata

    class Client:
        def __init__(self, *args) -> None:
            pass

        def get_pull_request(self, pr_number):
            return PullRequestMetadata(pr_number, title, "a" * 40, "b" * 40, "owner/repo", draft, labels)

        def list_changed_files(self, pr_number):
            return files

    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", Client)

    assert main(_metadata_args(tmp_path, "--allow-skip")) == 0
    outputs = _metadata_outputs(tmp_path)
    assert outputs["skip"] == ("true" if reason else "false")
    assert outputs["skip_reason"] == reason


def test_metadata_never_skips_without_allow_skip(monkeypatch, tmp_path) -> None:
    from gitea_auto_reviewer.gitea import PullRequestMetadata

    class Client:
        def __init__(self, *args) -> None:
            pass

        def get_pull_request(self, pr_number):
            return PullRequestMetadata(pr_number, "WIP", "a" * 40, "b" * 40, "owner/repo", True)

        def list_changed_files(self, pr_number):
            raise AssertionError("manual reviews do not inspect skip rules")

    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", Client)

    assert main(_metadata_args(tmp_path)) == 0
    assert _metadata_outputs(tmp_path)["skip"] == "false"


def test_feedback_summarizes_reactions_on_bot_comments(monkeypatch, tmp_path, capsys) -> None:
    from gitea_auto_reviewer.review import Review, render_markdown
    from test_review import impact_payload

    body = render_markdown(Review.from_json(json.dumps(impact_payload())), 3, "a" * 40, "t")

    class Client:
        def __init__(self, *args) -> None:
            pass

        def get_authenticated_login(self):
            return "bot"

        def list_pull_request_numbers(self, limit):
            assert limit == 10
            return (3, 2, 1)

        def find_review_comment(self, pr_number, author):
            assert author == "bot"
            return {3: (30, body), 2: (20, "not a review"), 1: None}[pr_number]

        def get_comment_reactions(self, comment_id):
            assert comment_id == 30
            return ("+1", "+1", "-1", "laugh")

    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", Client)
    output = tmp_path / "feedback.json"

    assert main(["feedback", "--gitea-url", "https://gitea.example", "--repository", "owner/repo",
                 "--limit", "10", "--output", str(output)]) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["reviews"] == [{
        "pr": 3, "head_sha": "a" * 40, "reproduced": 0, "unreproduced": 1, "rejected": 0,
        "thumbs_up": 2, "thumbs_down": 1,
    }]
    assert result["totals"]["reviews_with_feedback"] == 1
    assert "positive 66.7%" in capsys.readouterr().out


def test_feedback_requires_bot_identity(monkeypatch, tmp_path, capsys) -> None:
    class Client:
        def __init__(self, *args) -> None:
            pass

        def get_authenticated_login(self):
            return None

    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", Client)

    assert main(["feedback", "--gitea-url", "https://gitea.example", "--repository", "owner/repo",
                 "--output", str(tmp_path / "f.json")]) == 1
    assert "read:user" in capsys.readouterr().err


def _forbid(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not run in this stage")
    return fail


def test_comment_stage_never_runs_codex_or_child_processes(monkeypatch, tmp_path) -> None:
    import subprocess

    FakeGiteaClient.instances.clear()
    for name in ("run", "Popen", "check_output", "call"):
        monkeypatch.setattr(subprocess, name, _forbid(f"subprocess.{name}"))
    for name in ("assert_logged_in", "run_codex_review", "plan_reproductions",
                 "run_reproductions", "retry_inconclusive_reproductions", "verify_reproductions",
                 "collect_evidence", "index_repository", "collect_context"):
        monkeypatch.setattr(f"gitea_auto_reviewer.cli.{name}", _forbid(name))
    monkeypatch.setenv("GITEA_REVIEW_TOKEN", "token")
    monkeypatch.setattr("gitea_auto_reviewer.cli.GiteaClient", FakeGiteaClient)

    assert main(_comment_args(_review_file(tmp_path))) == 0
    assert len(FakeGiteaClient.instances[0].posted) == 1


def _review_args(tmp_path, head_repository="owner/repo"):
    evidence = tmp_path / "evidence.json"
    check = {"status": "not_run", "summary": "", "passed": None, "total": None}
    evidence.write_text(json.dumps({"version": 1, "head_sha": "c" * 40, "checks": {
        "django_check": check, "migration_check": check, "pytest": check,
    }}), encoding="utf-8")
    return ["review", "--repository", "owner/repo", "--head-repository", head_repository,
            "--pr", "7", "--pr-title", "t", "--base-sha", "a" * 40, "--head-sha", "b" * 40,
            "--repo-dir", str(tmp_path), "--evidence-file", str(evidence),
            "--output", str(tmp_path / "review.json")]


def test_review_stage_rejects_fork_pull_requests_before_reading_code(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("gitea_auto_reviewer.cli.collect_context", _forbid("collect_context"))
    monkeypatch.setattr("gitea_auto_reviewer.cli.run_codex_review", _forbid("run_codex_review"))

    assert main(_review_args(tmp_path, head_repository="fork/repo")) == 1
    assert "same repository" in capsys.readouterr().err


def test_review_stage_rejects_evidence_for_another_head_before_codex(monkeypatch, tmp_path, capsys) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr("gitea_auto_reviewer.cli.collect_context",
                        lambda *args: SimpleNamespace(head_sha="b" * 40))
    monkeypatch.setattr("gitea_auto_reviewer.cli.assert_logged_in", _forbid("assert_logged_in"))
    monkeypatch.setattr("gitea_auto_reviewer.cli.run_codex_review", _forbid("run_codex_review"))

    assert main(_review_args(tmp_path)) == 1
    assert "different PR head SHA" in capsys.readouterr().err
    assert not (tmp_path / "review.json").exists()


@pytest.mark.parametrize("command", ["review", "plan", "verify"])
def test_codex_stages_have_no_gitea_token_option(command) -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    options = {option for action in subparsers.choices[command]._actions for option in action.option_strings}
    assert "--token-env" not in options and "--gitea-url" not in options
