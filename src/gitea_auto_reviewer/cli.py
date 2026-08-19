"""Command-line entry point with separated review and comment stages."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .codex import assert_logged_in, run_codex_review
from .evidence import Evidence, collect_evidence
from .git_context import build_prompt, build_verification_prompt, collect_context, validate_sha
from .gitea import GiteaClient
from .review import Review, TestResult, render_markdown, validate_grounding, validate_verification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitea-auto-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    evidence = subparsers.add_parser("evidence", help="run deterministic checks in isolated CI")
    evidence.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    evidence.add_argument("--repo-dir", type=Path, default=Path.cwd())
    evidence.add_argument("--output", type=Path, default=Path("evidence.json"))
    evidence.add_argument("--python", default="python")
    evidence.add_argument("--timeout", type=int, default=900)

    review = subparsers.add_parser("review", help="create a review.json with Codex")
    review.add_argument("--repository", default=os.getenv("GITEA_REPOSITORY"), required=False)
    review.add_argument("--head-repository", default=os.getenv("GITEA_HEAD_REPOSITORY"))
    review.add_argument("--pr", type=int, default=_env_int("GITEA_PR_NUMBER"))
    review.add_argument("--pr-title", default=os.getenv("GITEA_PR_TITLE"))
    review.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    review.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    review.add_argument("--repo-dir", type=Path, default=Path.cwd())
    review.add_argument("--output", type=Path, default=Path("review.json"))
    review.add_argument("--evidence-file", type=Path, required=True)
    review.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    review.add_argument("--max-diff-bytes", type=int, default=1_000_000)

    comment = subparsers.add_parser("comment", help="validate and post a review.json")
    comment.add_argument("--gitea-url", default=os.getenv("GITEA_URL"))
    comment.add_argument("--repository", default=os.getenv("GITEA_REPOSITORY"))
    comment.add_argument("--pr", type=int, default=_env_int("GITEA_PR_NUMBER"))
    comment.add_argument("--pr-title", default=os.getenv("GITEA_PR_TITLE"))
    comment.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    comment.add_argument("--review-file", type=Path, default=Path("review.json"))
    comment.add_argument("--token-env", default="GITEA_REVIEW_TOKEN")
    return parser


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _required(value: object, name: str):
    if value is None or value == "":
        raise ValueError(f"{name} is required")
    return value


def review_command(arguments: argparse.Namespace) -> None:
    repository = str(_required(arguments.repository, "repository"))
    head_repository = str(_required(arguments.head_repository, "head repository"))
    if repository != head_repository:
        raise ValueError("v0.1 only reviews pull requests from the same repository")
    pr_number = int(_required(arguments.pr, "PR number"))
    if pr_number < 1:
        raise ValueError("PR number must be positive")
    context = collect_context(
        arguments.repo_dir.resolve(),
        str(_required(arguments.base_sha, "base SHA")),
        str(_required(arguments.head_sha, "head SHA")),
        arguments.max_diff_bytes,
    )
    evidence = Evidence.from_json(arguments.evidence_file.read_text(encoding="utf-8"))
    if evidence.head_sha != context.head_sha:
        raise ValueError("evidence belongs to a different PR head SHA")
    assert_logged_in(arguments.codex_binary)
    pr_title = str(_required(arguments.pr_title, "PR title"))
    prompt = build_prompt(context, repository, pr_number, pr_title, evidence.to_json())
    fixed_fields = {
        "changed_files": context.changed_files,
        "changed_file_paths": list(context.changed_file_paths),
        "django_check": evidence.django_check.status,
        "migration": {"pass": "no_missing", "fail": "missing", "error": "error", "not_run": "not_run"}[
            evidence.migration_check.status
        ],
        "tests": asdict(_evidence_tests(evidence)),
    }
    draft = run_codex_review(
        prompt,
        arguments.repo_dir.resolve(),
        arguments.codex_binary,
        fixed_fields=fixed_fields,
    )
    result = run_codex_review(
        build_verification_prompt(prompt, draft.to_json()),
        arguments.repo_dir.resolve(),
        arguments.codex_binary,
        fixed_fields=fixed_fields,
        reasoning_effort="low",
    )
    validate_verification(draft, result)
    result = replace(
        draft,
        risk=result.risk,
        risk_confidence=result.risk_confidence,
        risk_evidence=result.risk_evidence,
        findings=result.findings,
        external_integration=result.external_integration,
        external_integration_reason=result.external_integration_reason,
        external_integration_evidence=result.external_integration_evidence,
    )
    validate_grounding(result, arguments.repo_dir.resolve(), context.policy)
    arguments.output.write_text(result.to_json() + "\n", encoding="utf-8")
    print(f"Review written to {arguments.output}")


def evidence_command(arguments: argparse.Namespace) -> None:
    if arguments.timeout < 1:
        raise ValueError("timeout must be positive")
    evidence = collect_evidence(
        arguments.repo_dir.resolve(),
        str(_required(arguments.head_sha, "head SHA")),
        arguments.python,
        arguments.timeout,
    )
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Evidence written to {arguments.output}")


def _evidence_tests(evidence: Evidence) -> TestResult:
    check = evidence.pytest
    if check.status in {"pass", "fail"} and check.passed is not None and check.total is not None:
        return TestResult(check.status, check.passed, check.total)
    return TestResult("error" if check.status != "not_run" else "not_run", None, None)


def comment_command(arguments: argparse.Namespace) -> None:
    token = os.getenv(arguments.token_env)
    if not token and arguments.token_env == "GITEA_REVIEW_TOKEN":
        token = os.getenv("GITEA_TOKEN")
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    pr_number = int(_required(arguments.pr, "PR number"))
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    pr_title = str(_required(arguments.pr_title, "PR title"))
    client = GiteaClient(
        str(_required(arguments.gitea_url, "Gitea URL")),
        str(_required(arguments.repository, "repository")),
        str(_required(token, arguments.token_env)),
    )
    action = client.upsert_comment(
        pr_number, render_markdown(review, pr_number, head_sha, pr_title)
    )
    print(f"PR comment {action}")


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "evidence":
            evidence_command(arguments)
        elif arguments.command == "review":
            review_command(arguments)
        else:
            comment_command(arguments)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
