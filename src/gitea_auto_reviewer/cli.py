"""Command-line entry point with separated review and comment stages."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .codex import assert_logged_in, run_codex_review
from .evidence import Evidence, collect_evidence, merge_evidence
from .git_context import build_prompt, collect_context, validate_sha
from .gitea import GiteaClient
from .gitnexus import index_repository
from .review import Review, TestResult, render_markdown, validate_grounding
from .reproduction import (ReproductionEvidence, ReproductionPlan, VerificationDecision,
                           finalize_review, plan_reproductions, run_reproductions,
                           verify_reproductions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitea-auto-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="index the exact PR head with GitNexus")
    index.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    index.add_argument("--repo-dir", type=Path, default=Path.cwd())
    index.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    index.add_argument("--timeout", type=int, default=900)

    metadata = subparsers.add_parser("metadata", help="resolve a PR for automatic or manual review")
    metadata.add_argument("--gitea-url", default=os.getenv("GITEA_URL"))
    metadata.add_argument("--repository", default=os.getenv("GITEA_REPOSITORY"))
    metadata.add_argument("--pr", type=int, required=True)
    metadata.add_argument("--output-file", type=Path, required=True)
    metadata.add_argument("--token-env", default="GITEA_REVIEW_TOKEN")

    evidence = subparsers.add_parser("evidence", help="run deterministic checks in isolated CI")
    evidence.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    evidence.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    evidence.add_argument("--repo-dir", type=Path, default=Path.cwd())
    evidence.add_argument("--output", type=Path, default=Path("evidence.json"))
    evidence.add_argument("--python", default="python")
    evidence.add_argument("--timeout", type=int, default=900)
    evidence.add_argument("--only", choices=["django_check", "migration_check", "pytest"])

    evidence_merge = subparsers.add_parser("evidence-merge", help="combine SHA-bound check results")
    evidence_merge.add_argument("--input", type=Path, action="append", required=True)
    evidence_merge.add_argument("--output", type=Path, default=Path("evidence.json"))

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
    review.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    review.add_argument("--max-diff-bytes", type=int, default=1_000_000)

    plan = subparsers.add_parser("plan", help="ask Codex for rollback-only reproduction cases")
    plan.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    plan.add_argument("--review-file", type=Path, required=True)
    plan.add_argument("--output", type=Path, default=Path("reproduction-plan.json"))
    plan.add_argument("--repo-dir", type=Path, default=Path.cwd())
    plan.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    plan.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))

    reproduce = subparsers.add_parser("reproduce", help="execute reproduction cases with forced rollback")
    reproduce.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    reproduce.add_argument("--plan-file", type=Path, required=True)
    reproduce.add_argument("--output", type=Path, default=Path("reproduction-evidence.json"))
    reproduce.add_argument("--repo-dir", type=Path, default=Path.cwd())
    reproduce.add_argument("--python", required=True)
    reproduce.add_argument("--timeout", type=int, default=180)
    reproduce.add_argument("--require-setting", action="append", default=[])

    verify = subparsers.add_parser("verify", help="adversarially verify reproduced findings")
    verify.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    verify.add_argument("--review-file", type=Path, required=True)
    verify.add_argument("--reproduction-file", type=Path, required=True)
    verify.add_argument("--output", type=Path, default=Path("verification.json"))
    verify.add_argument("--repo-dir", type=Path, default=Path.cwd())
    verify.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    verify.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))

    finalize = subparsers.add_parser("finalize", help="publish only rollback-verified findings")
    finalize.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    finalize.add_argument("--review-file", type=Path, required=True)
    finalize.add_argument("--reproduction-file", type=Path, required=True)
    finalize.add_argument("--verification-file", type=Path, required=True)
    finalize.add_argument("--output", type=Path, default=Path("review.json"))

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
        "reproduced_findings": [],
    }
    result = run_codex_review(
        prompt,
        arguments.repo_dir.resolve(),
        arguments.codex_binary,
        fixed_fields=fixed_fields,
        gitnexus_binary=arguments.gitnexus_binary,
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
        arguments.only,
        arguments.base_sha,
    )
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Evidence written to {arguments.output}")


def evidence_merge_command(arguments: argparse.Namespace) -> None:
    parts = [Evidence.from_json(path.read_text(encoding="utf-8")) for path in arguments.input]
    evidence = merge_evidence(parts)
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Combined evidence written to {arguments.output}")


def index_command(arguments: argparse.Namespace) -> None:
    index_repository(
        arguments.repo_dir,
        str(_required(arguments.head_sha, "head SHA")),
        arguments.gitnexus_binary,
        arguments.timeout,
    )
    print("GitNexus index is ready")


def metadata_command(arguments: argparse.Namespace) -> None:
    token = os.getenv(arguments.token_env)
    client = GiteaClient(str(_required(arguments.gitea_url, "Gitea URL")),
                         str(_required(arguments.repository, "repository")),
                         str(_required(token, arguments.token_env)))
    metadata = client.get_pull_request(arguments.pr)
    repository = str(arguments.repository)
    if metadata.head_repository != repository:
        raise ValueError("v0.2 only reviews pull requests from the same repository")
    values = {
        "pr_number": str(metadata.number),
        "pr_title": " ".join(metadata.title.split()),
        "base_sha": validate_sha(metadata.base_sha),
        "head_sha": validate_sha(metadata.head_sha),
        "head_repository": metadata.head_repository,
    }
    with arguments.output_file.open("a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in values.items())
    print(f"Resolved PR #{metadata.number} at {values['head_sha']}")


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


def plan_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    assert_logged_in(arguments.codex_binary)
    plan = plan_reproductions(review, head_sha, arguments.repo_dir.resolve(),
                              arguments.codex_binary, arguments.gitnexus_binary)
    arguments.output.write_text(plan.to_json() + "\n", encoding="utf-8")
    print(f"Reproduction plan written to {arguments.output}")


def reproduce_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    plan = ReproductionPlan.from_json(arguments.plan_file.read_text(encoding="utf-8"), 5)
    if plan.head_sha != head_sha:
        raise ValueError("reproduction plan belongs to a different PR head SHA")
    evidence = run_reproductions(plan, arguments.repo_dir.resolve(), arguments.python,
                                 arguments.timeout, tuple(arguments.require_setting))
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Reproduction evidence written to {arguments.output}")


def finalize_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    evidence = ReproductionEvidence.from_json(arguments.reproduction_file.read_text(encoding="utf-8"))
    decision = VerificationDecision.from_json(arguments.verification_file.read_text(encoding="utf-8"), evidence)
    if evidence.head_sha != head_sha:
        raise ValueError("reproduction evidence belongs to a different PR head SHA")
    if decision.head_sha != head_sha:
        raise ValueError("verification belongs to a different PR head SHA")
    arguments.output.write_text(finalize_review(review, evidence, decision).to_json() + "\n", encoding="utf-8")
    print(f"Final review written to {arguments.output}")


def verify_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    evidence = ReproductionEvidence.from_json(arguments.reproduction_file.read_text(encoding="utf-8"))
    if evidence.head_sha != head_sha:
        raise ValueError("reproduction evidence belongs to a different PR head SHA")
    assert_logged_in(arguments.codex_binary)
    decision = verify_reproductions(review, evidence, arguments.repo_dir.resolve(),
                                    arguments.codex_binary, arguments.gitnexus_binary)
    arguments.output.write_text(decision.to_json() + "\n", encoding="utf-8")
    print(f"Verification written to {arguments.output}")


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "index":
            index_command(arguments)
        elif arguments.command == "metadata":
            metadata_command(arguments)
        elif arguments.command == "evidence":
            evidence_command(arguments)
        elif arguments.command == "evidence-merge":
            evidence_merge_command(arguments)
        elif arguments.command == "review":
            review_command(arguments)
        elif arguments.command == "plan":
            plan_command(arguments)
        elif arguments.command == "reproduce":
            reproduce_command(arguments)
        elif arguments.command == "verify":
            verify_command(arguments)
        elif arguments.command == "finalize":
            finalize_command(arguments)
        else:
            comment_command(arguments)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
