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
