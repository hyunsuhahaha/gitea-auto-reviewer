"""Command-line entry point with separated review and comment stages."""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .codex import assert_logged_in, run_codex_review
from .config import CONFIG_PATH, ReviewConfig, load_base_config, parse_config
from .differential import base_worktree, differential_blocker
from .evidence import Evidence, EnvironmentPolicy, collect_evidence, merge_evidence
from .git_context import build_prompt, collect_context, validate_sha
from .gitea import GiteaClient, PullRequestMetadata
from .gitnexus import GitNexusImpact, index_repository
from .messages import text
from .impact import analyze_impact, load_paths
from .review import (Review, TestResult, parse_previous_review, preserve_reproduced_findings,
                     render_markdown, validate_grounding)
from .reproduction import (ReproductionEvidence, ReproductionPlan, VerificationDecision,
                           finalize_review, plan_reproductions, run_reproductions,
                           retry_inconclusive_reproductions, verify_reproductions)


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
    metadata.add_argument("--allow-skip", action="store_true",
                          help="skip draft, labeled, or documentation-only PRs")
    metadata.add_argument("--skip-label", help=f"override [skip].label from {CONFIG_PATH}")
    metadata.add_argument("--config-output", type=Path,
                          help=f"write the base commit's resolved {CONFIG_PATH} as JSON")

    config = subparsers.add_parser("config", help=f"resolve {CONFIG_PATH} from the trusted base commit")
    config.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    config.add_argument("--repo-dir", type=Path, default=Path.cwd())
    config.add_argument("--output", type=Path, default=Path("ai-review-config.json"))

    evidence = subparsers.add_parser("evidence", help="run deterministic checks in isolated CI")
    evidence.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    evidence.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    evidence.add_argument("--repo-dir", type=Path, default=Path.cwd())
    evidence.add_argument("--output", type=Path, default=Path("evidence.json"))
    evidence.add_argument("--python", default="python")
    evidence.add_argument("--timeout", type=int, default=900)
    evidence.add_argument("--only", choices=["django_check", "migration_check", "pytest"])
    evidence.add_argument("--trace-output", type=Path,
                          help="record pytest call chains through changed functions (requires --base-sha)")
    _config_option(evidence)

    evidence_merge = subparsers.add_parser("evidence-merge", help="combine SHA-bound check results")
    evidence_merge.add_argument("--input", type=Path, action="append", required=True)
    evidence_merge.add_argument("--output", type=Path, default=Path("evidence.json"))

    impact = subparsers.add_parser("impact", help="find Django/ORM/runtime impact paths a static graph misses")
    impact.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    impact.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    impact.add_argument("--repo-dir", type=Path, default=Path.cwd())
    impact.add_argument("--python", required=True)
    impact.add_argument("--runtime-trace", type=Path)
    impact.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    impact.add_argument("--skip-gitnexus", action="store_true")
    impact.add_argument("--output", type=Path, default=Path("impact.json"))
    _config_option(impact)

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
    review.add_argument("--impact-file", type=Path)
    review.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    review.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    review.add_argument("--reasoning-effort", choices=EFFORT_CHOICES)
    review.add_argument("--max-diff-bytes", type=int, default=10_000_000)
    _config_option(review)

    plan = subparsers.add_parser("plan", help="ask Codex for rollback-only reproduction cases")
    plan.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    plan.add_argument("--review-file", type=Path, required=True)
    plan.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    plan.add_argument("--output", type=Path, default=Path("reproduction-plan.json"))
    plan.add_argument("--repo-dir", type=Path, default=Path.cwd())
    plan.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    plan.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    plan.add_argument("--reasoning-effort", choices=EFFORT_CHOICES)
    _config_option(plan)

    reproduce = subparsers.add_parser("reproduce", help="execute reproduction cases with forced rollback")
    reproduce.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    reproduce.add_argument("--plan-file", type=Path, required=True)
    reproduce.add_argument("--base-sha", default=os.getenv("GITEA_BASE_SHA"))
    reproduce.add_argument("--output", type=Path, default=Path("reproduction-evidence.json"))
    reproduce.add_argument("--repo-dir", type=Path, default=Path.cwd())
    reproduce.add_argument("--python", required=True)
    reproduce.add_argument("--timeout", type=int, help="seconds per case (default: config or 180)")
    reproduce.add_argument("--require-setting", action="append", default=[])
    reproduce.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    reproduce.add_argument("--gitnexus-binary", default=os.getenv(
        "GITNEXUS_BINARY", os.getenv("GITNEXUS_EXE", "gitnexus")
    ))
    reproduce.add_argument("--repair-reasoning-effort", choices=EFFORT_CHOICES)
    _config_option(reproduce)

    verify = subparsers.add_parser("verify", help="adversarially verify reproduced findings")
    verify.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    verify.add_argument("--review-file", type=Path, required=True)
    verify.add_argument("--reproduction-file", type=Path, required=True)
    verify.add_argument("--output", type=Path, default=Path("verification.json"))
    verify.add_argument("--repo-dir", type=Path, default=Path.cwd())
    verify.add_argument("--codex-binary", default=os.getenv("CODEX_BINARY", "codex"))
    verify.add_argument("--gitnexus-binary", default=os.getenv("GITNEXUS_BINARY", "gitnexus"))
    verify.add_argument("--reasoning-effort", choices=EFFORT_CHOICES)
    _config_option(verify)

    reasoning = subparsers.add_parser("reasoning", help="show the configured Codex reasoning efforts")
    _config_option(reasoning)

    finalize = subparsers.add_parser("finalize", help="publish only rollback-verified findings")
    finalize.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    finalize.add_argument("--review-file", type=Path, required=True)
    finalize.add_argument("--reproduction-file", type=Path, required=True)
    finalize.add_argument("--verification-file", type=Path, required=True)
    finalize.add_argument("--output", type=Path, default=Path("review.json"))
    _config_option(finalize)

    comment = subparsers.add_parser("comment", help="validate and post a review.json")
    comment.add_argument("--gitea-url", default=os.getenv("GITEA_URL"))
    comment.add_argument("--repository", default=os.getenv("GITEA_REPOSITORY"))
    comment.add_argument("--pr", type=int, default=_env_int("GITEA_PR_NUMBER"))
    comment.add_argument("--pr-title", default=os.getenv("GITEA_PR_TITLE"))
    comment.add_argument("--head-sha", default=os.getenv("GITEA_HEAD_SHA"))
    comment.add_argument("--review-file", type=Path, default=Path("review.json"))
    comment.add_argument("--token-env", default="GITEA_REVIEW_TOKEN")
    _config_option(comment)

    feedback = subparsers.add_parser("feedback", help="summarize reactions on recent bot comments")
    feedback.add_argument("--gitea-url", default=os.getenv("GITEA_URL"))
    feedback.add_argument("--repository", default=os.getenv("GITEA_REPOSITORY"))
    feedback.add_argument("--limit", type=int, default=50, help="number of recent PRs to inspect")
    feedback.add_argument("--output", type=Path, default=Path("feedback.json"))
    feedback.add_argument("--token-env", default="GITEA_REVIEW_TOKEN")
    return parser


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


EFFORT_CHOICES = ["low", "medium", "high"]


def _config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-file", type=Path, default=_env_path("AI_REVIEW_CONFIG_FILE"),
                        help="resolved config JSON from `metadata --config-output` or `config`")


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


def _load_config(arguments: argparse.Namespace) -> ReviewConfig:
    path = getattr(arguments, "config_file", None)
    if path is None:
        return ReviewConfig()
    return ReviewConfig.from_json(path.read_text(encoding="utf-8"))


def _reasoning_effort(name: str, fallback: str, explicit: str | None = None) -> str:
    """Resolve an effort: CLI option, then the Actions variable, then the base config."""
    value = explicit or os.getenv(name, "").strip().lower() or fallback
    if value not in EFFORT_CHOICES:
        raise ValueError(f"{name} must be low, medium, or high")
    return value


def _environment_policy(config: ReviewConfig) -> EnvironmentPolicy:
    return EnvironmentPolicy(config.pass_env, config.django_settings_module)


WIP_PREFIXES = ("wip:", "[wip]", "wip ")


def _skip_reason(metadata: PullRequestMetadata, changed_files: tuple[str, ...] | None,
                 skip_label: str, config: ReviewConfig = ReviewConfig()) -> str | None:
    if skip_label and skip_label in metadata.labels:
        return f"{skip_label} label"
    if config.skip_drafts and (metadata.draft or metadata.title.strip().lower().startswith(WIP_PREFIXES)):
        return "draft pull request"
    if changed_files and config.documentation_paths and all(
        any(fnmatch.fnmatchcase(path, pattern) for pattern in config.documentation_paths)
        for path in changed_files
    ):
        return "documentation-only change"
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
    impact_paths = []
    impact_json = None
    if arguments.impact_file is not None:
        impact_json = arguments.impact_file.read_text(encoding="utf-8")
        impact_paths = [asdict(item) for item in load_paths(impact_json, context.head_sha)]
    config = _load_config(arguments)
    prompt = build_prompt(context, repository, pr_number, pr_title, evidence.to_json(), impact_json,
                          config.language)
    fixed_fields = {
        "changed_files": context.changed_files,
        "changed_file_paths": list(context.changed_file_paths),
        "django_check": evidence.django_check.status,
        "migration": {"pass": "no_missing", "fail": "missing", "error": "error", "not_run": "not_run"}[
            evidence.migration_check.status
        ],
        "tests": asdict(_evidence_tests(evidence)),
        "reproduced_findings": [],
        "impact_paths": impact_paths,
    }
    result = run_codex_review(
        prompt,
        arguments.repo_dir.resolve(),
        arguments.codex_binary,
        fixed_fields=fixed_fields,
        reasoning_effort=_reasoning_effort("AI_REVIEW_FIRST_PASS_EFFORT", config.first_pass_effort,
                                           arguments.reasoning_effort),
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
        arguments.trace_output,
        _environment_policy(_load_config(arguments)),
    )
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Evidence written to {arguments.output}")


def evidence_merge_command(arguments: argparse.Namespace) -> None:
    parts = [Evidence.from_json(path.read_text(encoding="utf-8")) for path in arguments.input]
    evidence = merge_evidence(parts)
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Combined evidence written to {arguments.output}")


def impact_command(arguments: argparse.Namespace) -> None:
    repository = arguments.repo_dir.resolve()
    base_sha = validate_sha(str(_required(arguments.base_sha, "base SHA")))
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    runtime = None
    if arguments.runtime_trace is not None and arguments.runtime_trace.exists():
        runtime = json.loads(arguments.runtime_trace.read_text(encoding="utf-8"))
    with contextlib.ExitStack() as stack:
        gitnexus = None
        if not arguments.skip_gitnexus:
            try:
                gitnexus = GitNexusImpact(arguments.gitnexus_binary, repository)
                stack.callback(gitnexus.close)
            except RuntimeError as exc:
                print(f"GitNexus comparison skipped: {exc}", file=sys.stderr)
        config = _load_config(arguments)
        document = analyze_impact(repository, base_sha, head_sha, arguments.python, runtime, gitnexus,
                                  _environment_policy(config), config.language)
    arguments.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    missed = sum(1 for item in document["paths"] if item["static_missed"])
    print(f"Impact paths written to {arguments.output}: {len(document['paths'])} paths, "
          f"{missed} absent from GitNexus ({document['sources']})")


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
    # Read the config at the base SHA so a pull request cannot loosen its own review settings.
    config = parse_config(client.get_file_text(CONFIG_PATH, values["base_sha"]))
    if arguments.config_output is not None:
        arguments.config_output.write_text(config.to_json() + "\n", encoding="utf-8")
    reason = None
    if arguments.allow_skip:
        label = config.skip_label if arguments.skip_label is None else arguments.skip_label
        reason = _skip_reason(metadata, client.list_changed_files(metadata.number), label, config)
    values["skip"] = "true" if reason else "false"
    values["skip_reason"] = reason or ""
    with arguments.output_file.open("a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in values.items())
    print(f"Resolved PR #{metadata.number} at {values['head_sha']}")
    if reason:
        print(f"Skipping AI review: {reason}")


def config_command(arguments: argparse.Namespace) -> None:
    config = load_base_config(arguments.repo_dir.resolve(), str(_required(arguments.base_sha, "base SHA")))
    arguments.output.write_text(config.to_json() + "\n", encoding="utf-8")
    print(f"Resolved config written to {arguments.output}")


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
    current_head = client.get_pull_request(pr_number).head_sha
    if current_head != head_sha:
        # A newer push started its own review; never let this older run overwrite it.
        print(f"PR head moved to {current_head}; skipped stale review for {head_sha}")
        return
    # Hidden review state is only trusted from the bot's own comment; anyone can copy the marker.
    author = client.get_authenticated_login()
    previous = client.get_review_comment(pr_number, author) if author else None
    review = preserve_reproduced_findings(review, previous, head_sha)
    body = render_markdown(review, pr_number, head_sha, pr_title, parse_previous_review(previous),
                           _load_config(arguments).language)
    action = client.upsert_comment(pr_number, body, author)
    print(f"PR comment {action}")


def feedback_command(arguments: argparse.Namespace) -> None:
    if arguments.limit < 1:
        raise ValueError("limit must be positive")
    token = os.getenv(arguments.token_env)
    client = GiteaClient(str(_required(arguments.gitea_url, "Gitea URL")),
                         str(_required(arguments.repository, "repository")),
                         str(_required(token, arguments.token_env)))
    author = client.get_authenticated_login()
    if author is None:
        raise ValueError("feedback needs a token with read:user to identify bot comments")
    reviews = []
    for number in client.list_pull_request_numbers(arguments.limit):
        comment = client.find_review_comment(number, author)
        state = parse_previous_review(comment[1]) if comment else None
        if comment is None or state is None:
            continue
        reactions = client.get_comment_reactions(comment[0])
        reviews.append({
            "pr": number,
            "head_sha": state.head_sha,
            "reproduced": len(state.reproduced_findings),
            "unreproduced": state.unreproduced_count,
            "rejected": state.rejected_count,
            "thumbs_up": reactions.count("+1"),
            "thumbs_down": reactions.count("-1"),
        })
    totals = {
        "reviews": len(reviews),
        "reviews_with_feedback": sum(1 for item in reviews if item["thumbs_up"] or item["thumbs_down"]),
        **{name: sum(item[name] or 0 for item in reviews)
           for name in ("reproduced", "unreproduced", "rejected", "thumbs_up", "thumbs_down")},
    }
    arguments.output.write_text(
        json.dumps({"totals": totals, "reviews": reviews}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    votes = totals["thumbs_up"] + totals["thumbs_down"]
    rate = f"{totals['thumbs_up'] / votes * 100:.1f}%" if votes else "n/a"
    print(f"{totals['reviews']} reviews, {totals['reviews_with_feedback']} with feedback, "
          f"+1 {totals['thumbs_up']} / -1 {totals['thumbs_down']} (positive {rate})")
    print(f"Feedback written to {arguments.output}")


def plan_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    assert_logged_in(arguments.codex_binary)
    config = _load_config(arguments)
    blocker = _differential_blocker(arguments.repo_dir.resolve(), arguments.base_sha, head_sha, config.language)
    print(f"Differential execution: {'available' if blocker is None else blocker}")
    effort = _reasoning_effort("AI_REVIEW_PLAN_EFFORT", config.plan_effort, arguments.reasoning_effort)
    plan = plan_reproductions(review, head_sha, arguments.repo_dir.resolve(),
                              arguments.codex_binary, arguments.gitnexus_binary, effort, blocker,
                              config.language)
    arguments.output.write_text(plan.to_json() + "\n", encoding="utf-8")
    print(f"Reproduction plan written to {arguments.output}")


def reproduce_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    plan = ReproductionPlan.from_json(arguments.plan_file.read_text(encoding="utf-8"), 5)
    if plan.head_sha != head_sha:
        raise ValueError("reproduction plan belongs to a different PR head SHA")
    repository = arguments.repo_dir.resolve()
    config = _load_config(arguments)
    timeout = config.reproduction_timeout if arguments.timeout is None else arguments.timeout
    if timeout < 1:
        raise ValueError("timeout must be positive")
    required = tuple(dict.fromkeys((*config.required_settings, *arguments.require_setting)))
    policy = _environment_policy(config)
    effort = _reasoning_effort("AI_REVIEW_REPAIR_EFFORT", config.repair_effort,
                               arguments.repair_reasoning_effort)
    needs_base = any(case.mode == "differential" for case in plan.cases)
    blocker = (_differential_blocker(repository, arguments.base_sha, head_sha, config.language)
               if needs_base else None)
    with contextlib.ExitStack() as stack:
        base = (stack.enter_context(base_worktree(repository, arguments.base_sha, head_sha))
                if needs_base and blocker is None else None)
        evidence = run_reproductions(plan, repository, arguments.python, timeout,
                                     required, base, blocker, policy, config.language)
        evidence = retry_inconclusive_reproductions(
            plan, evidence, repository, arguments.python, timeout,
            required, arguments.codex_binary, arguments.gitnexus_binary,
            effort, base, blocker, policy, config.language,
        )
    arguments.output.write_text(evidence.to_json() + "\n", encoding="utf-8")
    print(f"Reproduction evidence written to {arguments.output}")


def _differential_blocker(repository: Path, base_sha: str | None, head_sha: str,
                          language: str = "en") -> str | None:
    if not base_sha:
        return text(language, "no_base_sha")
    return differential_blocker(repository, base_sha, head_sha, language)


def finalize_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    evidence = ReproductionEvidence.from_json(arguments.reproduction_file.read_text(encoding="utf-8"))
    language = _load_config(arguments).language
    decision = VerificationDecision.from_json(arguments.verification_file.read_text(encoding="utf-8"), evidence,
                                              language)
    if evidence.head_sha != head_sha:
        raise ValueError("reproduction evidence belongs to a different PR head SHA")
    if decision.head_sha != head_sha:
        raise ValueError("verification belongs to a different PR head SHA")
    arguments.output.write_text(finalize_review(review, evidence, decision, language).to_json() + "\n",
                                encoding="utf-8")
    print(f"Final review written to {arguments.output}")


def verify_command(arguments: argparse.Namespace) -> None:
    head_sha = validate_sha(str(_required(arguments.head_sha, "head SHA")))
    review = Review.from_json(arguments.review_file.read_text(encoding="utf-8"))
    evidence = ReproductionEvidence.from_json(arguments.reproduction_file.read_text(encoding="utf-8"))
    if evidence.head_sha != head_sha:
        raise ValueError("reproduction evidence belongs to a different PR head SHA")
    assert_logged_in(arguments.codex_binary)
    config = _load_config(arguments)
    effort = _reasoning_effort("AI_REVIEW_VERIFY_EFFORT", config.verify_effort, arguments.reasoning_effort)
    decision = verify_reproductions(review, evidence, arguments.repo_dir.resolve(),
                                    arguments.codex_binary, arguments.gitnexus_binary, effort, config.language)
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
        elif arguments.command == "impact":
            impact_command(arguments)
        elif arguments.command == "reproduce":
            reproduce_command(arguments)
        elif arguments.command == "verify":
            verify_command(arguments)
        elif arguments.command == "finalize":
            finalize_command(arguments)
        elif arguments.command == "feedback":
            feedback_command(arguments)
        elif arguments.command == "config":
            config_command(arguments)
        elif arguments.command == "reasoning":
            config = _load_config(arguments)
            print(f"first-pass={_reasoning_effort('AI_REVIEW_FIRST_PASS_EFFORT', config.first_pass_effort)}")
            print(f"plan={_reasoning_effort('AI_REVIEW_PLAN_EFFORT', config.plan_effort)}")
            print(f"verify={_reasoning_effort('AI_REVIEW_VERIFY_EFFORT', config.verify_effort)}")
            print(f"repair={_reasoning_effort('AI_REVIEW_REPAIR_EFFORT', config.repair_effort)}")
        else:
            comment_command(arguments)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
