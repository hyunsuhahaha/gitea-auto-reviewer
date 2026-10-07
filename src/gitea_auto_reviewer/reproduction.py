"""Plan and safely execute rollback-only Django finding reproductions."""

from __future__ import annotations

import ast
import configparser
import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .codex import run_codex_json
from .differential import compare_runs, validate_predictions
from .evidence import safe_evidence_environment
from .git_context import validate_sha
from .review import ReproducedFinding, Review

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "head_sha", "cases"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "head_sha": {"type": "string"},
        "cases": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["finding_index", "mode", "condition", "oracle", "predicted_changes", "script"],
            "properties": {
                "finding_index": {"type": "integer", "minimum": 0, "maximum": 4},
                "mode": {"enum": ["assert", "differential"]},
                "condition": {"type": "string", "minLength": 1, "maxLength": 1000},
                "oracle": {"type": "string", "minLength": 1, "maxLength": 1000},
                "predicted_changes": {
                    "type": "array", "maxItems": 10,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "script": {"type": "string", "minLength": 1, "maxLength": 50000},
            },
        }},
    },
}

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "head_sha", "accepted_finding_indices", "rejected_findings"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "head_sha": {"type": "string"},
        "accepted_finding_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 4},
        },
        "rejected_findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["finding_index", "reason"],
                "properties": {
                    "finding_index": {"type": "integer", "minimum": 0, "maximum": 4},
                    "reason": {"type": "string", "minLength": 10, "maxLength": 1000},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class ReproductionCase:
    finding_index: int
    condition: str
    oracle: str
    script: str
    target_evidence: tuple[str, ...] = ()
    mode: str = "assert"
    predicted_changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReproductionPlan:
    head_sha: str
    cases: tuple[ReproductionCase, ...]

    @classmethod
    def from_json(cls, raw: str, finding_count: int) -> "ReproductionPlan":
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "cases"} or value["version"] != 1:
            raise ValueError("invalid reproduction plan")
        cases = value["cases"]
        if not isinstance(cases, list):
            raise ValueError("reproduction plan cases must be a list")
        parsed: list[ReproductionCase] = []
        indexes: set[int] = set()
        for item in cases:
            required = {"finding_index", "condition", "oracle", "script"}
            optional = {"target_evidence", "mode", "predicted_changes"}
            if not isinstance(item, dict) or not required <= set(item) <= required | optional:
                raise ValueError("invalid reproduction case")
            index = item["finding_index"]
            if type(index) is not int or not 0 <= index < finding_count or index in indexes:
                continue
            condition, oracle, script = item["condition"], item["oracle"], item["script"]
            if not all(isinstance(text, str) and text.strip() for text in (condition, oracle, script)):
                raise ValueError("reproduction case text must not be empty")
            validate_script(script)
            targets = item.get("target_evidence", [])
            if not isinstance(targets, list) or len(targets) > 5:
                raise ValueError("invalid reproduction target evidence")
            normalized_targets = []
            for ref in targets:
                path, separator, line = ref.rpartition(":") if isinstance(ref, str) else ("", "", "")
                if not separator or not path.lower().endswith(".py") or not line.isdigit() or int(line) < 1:
                    raise ValueError("invalid reproduction target evidence")
                normalized_targets.append(f"{path.replace(chr(92), '/')}:{int(line)}")
            mode = item.get("mode", "assert")
            if mode not in {"assert", "differential"}:
                raise ValueError("invalid reproduction mode")
            predictions = validate_predictions(item.get("predicted_changes", []))
            if mode == "differential" and not predictions:
                raise ValueError("differential reproduction requires predicted_changes")
            parsed.append(ReproductionCase(
                index, condition.strip(), oracle.strip(), script, tuple(normalized_targets),
                mode, predictions if mode == "differential" else (),
            ))
            indexes.add(index)
        return cls(validate_sha(value["head_sha"]), tuple(parsed))

    def to_json(self) -> str:
        return json.dumps({"version": 1, "head_sha": self.head_sha, "cases": [asdict(case) for case in self.cases]}, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class ReproductionResult:
    finding_index: int
    status: str
    condition: str
    oracle: str
    expected: str
    observed: str
    cleanup_verified: bool
    duration_seconds: float
    population_label: str | None = None
    matching_count: int | None = None
    total_count: int | None = None
    target_reached: bool = False
    reached_targets: tuple[str, ...] = ()
    script: str = ""
    mode: str = "assert"
    predicted_differences: tuple[str, ...] = ()
    other_differences: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReproductionEvidence:
    head_sha: str
    results: tuple[ReproductionResult, ...]

    @classmethod
    def from_json(cls, raw: str) -> "ReproductionEvidence":
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "results"} or value["version"] != 1:
            raise ValueError("invalid reproduction evidence")
        results = tuple(ReproductionResult(**{
            **item, **{name: tuple(item.get(name, ())) for name in (
                "reached_targets", "predicted_differences", "other_differences")},
        }) for item in value["results"])
        if any(item.status not in {"confirmed", "refuted", "inconclusive"} for item in results):
            raise ValueError("invalid reproduction status")
        if any(type(item.target_reached) is not bool or any(
                not isinstance(ref, str) for ref in item.reached_targets) for item in results):
            raise ValueError("invalid reproduction target evidence")
        if any(not isinstance(item.script, str) or len(item.script) > 50000 for item in results):
            raise ValueError("invalid reproduction script evidence")
        if any(item.mode not in {"assert", "differential"} or any(
                not isinstance(line, str) for line in (*item.predicted_differences, *item.other_differences))
                for item in results):
            raise ValueError("invalid differential reproduction evidence")
        return cls(validate_sha(value["head_sha"]), results)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "head_sha": self.head_sha, "results": [asdict(item) for item in self.results]}, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class VerificationRejection:
    finding_index: int
    reason: str


@dataclass(frozen=True)
class VerificationDecision:
    head_sha: str
    accepted_finding_indices: tuple[int, ...]
    rejected_findings: tuple[VerificationRejection, ...] = ()

    @classmethod
    def from_json(cls, raw: str, evidence: ReproductionEvidence) -> "VerificationDecision":
        value = json.loads(raw)
        required = {"version", "head_sha", "accepted_finding_indices", "rejected_findings"}
        if not isinstance(value, dict) or set(value) != required or value["version"] != 1:
            raise ValueError("invalid reproduction verification")
        indexes = value["accepted_finding_indices"]
        confirmed = {item.finding_index for item in evidence.results
                     if item.status == "confirmed" and item.cleanup_verified and item.target_reached}
        if (not isinstance(indexes, list) or any(type(index) is not int for index in indexes)
                or len(indexes) != len(set(indexes)) or not set(indexes) <= confirmed):
            raise ValueError("verification may accept only confirmed findings")
        rejected = value["rejected_findings"]
        if not isinstance(rejected, list):
            raise ValueError("invalid reproduction rejection reasons")
        parsed_rejections: list[VerificationRejection] = []
        for item in rejected:
            if (not isinstance(item, dict) or set(item) != {"finding_index", "reason"}
                    or type(item["finding_index"]) is not int
                    or not isinstance(item["reason"], str) or len(item["reason"].strip()) < 10
                    or len(item["reason"].strip()) > 1000):
                raise ValueError("invalid reproduction rejection reasons")
            reason = item["reason"].strip()
            if (not any("가" <= char <= "힣" for char in reason)
                    or reason.lower() in {"insufficient evidence", "증거가 불충분함",
                                          "재현 결과가 문제와 영향을 입증하기에 불충분함"}):
                raise ValueError("rejection reason must be concrete Korean text")
            parsed_rejections.append(VerificationRejection(
                item["finding_index"], reason
            ))
        rejected_indexes = [item.finding_index for item in parsed_rejections]
        if (len(rejected_indexes) != len(set(rejected_indexes))
                or set(indexes).intersection(rejected_indexes)
                or set(indexes).union(rejected_indexes) != confirmed):
            raise ValueError("verification must explain every rejected confirmed finding")
        head_sha = validate_sha(value["head_sha"])
        if head_sha != evidence.head_sha:
            raise ValueError("verification belongs to a different PR head SHA")
        return cls(head_sha, tuple(indexes), tuple(parsed_rejections))

    def to_json(self) -> str:
        return json.dumps({"version": 1, "head_sha": self.head_sha,
                           "accepted_finding_indices": list(self.accepted_finding_indices),
                           "rejected_findings": [asdict(item) for item in self.rejected_findings]},
                          ensure_ascii=False, indent=2)


DIFFERENTIAL_PROMPT = """
Prefer mode `differential` for every data-integrity finding. In differential mode the fixed runner executes the same script twice against the trusted base commit and twice against the PR head, records every row the scenario inserts, updates, or deletes before the forced rollback, drops values that differ between identical runs, and compares base with head. Therefore:
- Call only entry points that exist with the same import path in both base and head, and build fixtures that are valid in both.
- Return `observed` as the JSON-compatible business result of the target call (never a verdict) and omit `expected`; cleanup_checks are optional because the runner verifies restoration of every written table.
- Let exceptions from the target call propagate; the runner records them as the scenario outcome.
- Set `predicted_changes` to the exact places that must differ from base if the finding is true: `Model`, `Model.field`, `app_label.Model.field`, `result`, or `exception`. A difference outside these places never confirms the finding.
Use mode `assert` with an empty `predicted_changes` only when the finding cannot be observed as a base/head behavior difference.
"""

ASSERT_ONLY_PROMPT = """
Use mode `assert` and an empty `predicted_changes` for every case. Base/head differential execution is unavailable: {reason}
"""


def build_plan_prompt(review: Review, head_sha: str,
                      differential_blocker: str | None = "base SHA가 제공되지 않음") -> str:
    mode_rules = (DIFFERENTIAL_PROMPT if differential_blocker is None
                  else ASSERT_ONLY_PROMPT.format(reason=differential_blocker))
    return f"""You are planning rollback-only reproductions for candidate code-review findings.
The repository is already checked out at PR head {head_sha}. Inspect it, including GitNexus MCP context.

Return one case for every finding that can be objectively reproduced by importing Django and directly calling ORM/service/view code against the configured test database. Skip subjective, destructive, external-network, browser-only, or schema-incompatible cases.
`finding_index` is the zero-based position in the candidate review's `findings` array. Return at most one case for each finding and never invent an index outside that array.

Write user-visible explanations such as `condition` and `oracle` in Korean. Return `expected` and `observed` as exact JSON-compatible business values rather than explanatory prose; keep code identifiers and concrete values unchanged. Write `condition` as 1-6 concise, unnumbered lines describing the minimal generalized data state and final action required for the bug; never combine them into a paragraph. Do not present arbitrary fixture values chosen by the reproduction script as required conditions. Omit exact quantities, IDs, dates, and ratios unless that exact value or boundary is causally required for the bug. Put chosen example values and calculations only in `observed`.

{mode_rules}
Each script must contain only imports plus exactly `def reproduce():`, and return a JSON-compatible dict:
  expected: the exact JSON-compatible value required by the oracle (assert mode only)
  observed: the exact JSON-compatible value produced by the target code
  cleanup_checks: non-empty list of {{model, lookup, field, equals}} or {{model, lookup, exists}}
  population_label, matching_count, total_count: prevalence data counted from untouched test-DB rows before any reproduction mutation. Return all three whenever ORM can define a defensible natural population and matching condition; omit all three only when it cannot. Never invent counts. These values are informational and never decide whether a finding is confirmed.

The fixed runner, not the script, decides confirmed/refuted by comparing expected and observed after proving that cited target code executed. Never return or calculate a confirmed verdict. The fixed runner supplies django.setup(), transaction.atomic(), forced rollback, a fresh-connection cleanup check, code-reach tracing, and exception handling. Do not manage transactions. Select existing records semantically through ORM; never hard-code database primary keys. Do not write files, spawn processes, use network clients, call ERP, or mutate anything outside the rollback transaction. RequestFactory/SimpleNamespace and direct Django view calls are allowed.
Use timezone-aware datetimes compatible with the repository settings. Prefer django.utils.timezone.now(); never pass a naive datetime to timezone.localtime() or timezone-aware model logic. The script must reach the candidate's changed business logic rather than failing during fixture construction.

Candidate review JSON:
{review.to_json()}
"""


def plan_reproductions(review: Review, head_sha: str, repository: Path, codex_binary: str,
                       gitnexus_binary: str, reasoning_effort: str = "medium",
                       differential_blocker: str | None = "base SHA가 제공되지 않음") -> ReproductionPlan:
    if not review.findings:
        return ReproductionPlan(validate_sha(head_sha), ())
    raw = run_codex_json(build_plan_prompt(review, head_sha, differential_blocker), PLAN_SCHEMA, repository, codex_binary,
                         fixed_fields={"version": 1, "head_sha": head_sha}, reasoning_effort=reasoning_effort,
                         gitnexus_binary=gitnexus_binary)
    plan = ReproductionPlan.from_json(raw, len(review.findings))
    cases = tuple(
        replace(case, target_evidence=tuple(
            ref for ref in review.findings[case.finding_index].evidence
            if ref.rpartition(":")[0].lower().endswith(".py")
        )) for case in plan.cases
    )
    return ReproductionPlan(plan.head_sha, tuple(case for case in cases if case.target_evidence))


def verify_reproductions(review: Review, evidence: ReproductionEvidence, repository: Path,
                         codex_binary: str, gitnexus_binary: str,
                         reasoning_effort: str = "low") -> VerificationDecision:
    confirmed = [item for item in evidence.results
                 if item.status == "confirmed" and item.cleanup_verified and item.target_reached]
    if not confirmed:
        return VerificationDecision(evidence.head_sha, (), ())
    prompt = f"""You are the second, adversarial verification pass for code-review findings.
Only the candidate findings listed in the reproduction evidence were executed against the test database.
For mode `differential`, the runner executed the same script against base and head and recorded `predicted_differences` (base/head differences at the places the plan predicted) and `other_differences`; reject when those differences are explained by the PR's intended behavior rather than the candidate problem.
For each confirmed result, inspect its executed reproduction script, the repository, and GitNexus again and try to disprove that the observed result supports the exact candidate problem and impact. Reject a result when observed is fabricated, is not derived from the reached target call or resulting DB state, or the target call is unrelated to the oracle. Accept an index only when the oracle is objective, expected and observed are genuinely comparable, the observation demonstrates that exact problem, target execution and cleanup were verified, and no concrete code path invalidates the conclusion. Never add, rewrite, or accept an unconfirmed finding. Put every confirmed index in exactly one of accepted_finding_indices or rejected_findings. For every rejection, write a concrete Korean reason naming the missing or contradictory evidence; never use a generic phrase such as "insufficient evidence".

Candidate review:
{review.to_json()}

Reproduction evidence:
{evidence.to_json()}
"""
    raw = run_codex_json(prompt, VERIFICATION_SCHEMA, repository, codex_binary,
                         fixed_fields={"version": 1, "head_sha": evidence.head_sha},
                         reasoning_effort=reasoning_effort, gitnexus_binary=gitnexus_binary)
    return VerificationDecision.from_json(raw, evidence)


def retry_inconclusive_reproductions(
    plan: ReproductionPlan,
    evidence: ReproductionEvidence,
    repository: Path,
    python: str,
    timeout: int,
    required_settings: tuple[str, ...],
    codex_binary: str,
    gitnexus_binary: str,
    reasoning_effort: str = "medium",
    base_repository: Path | None = None,
    differential_blocker: str | None = None,
) -> ReproductionEvidence:
    failed_indexes = {item.finding_index for item in evidence.results if item.status == "inconclusive"}
    if not failed_indexes:
        return evidence
    failed_cases = tuple(case for case in plan.cases if case.finding_index in failed_indexes)
    failed_results = tuple(item for item in evidence.results if item.finding_index in failed_indexes)
    prompt = f"""You are repairing rollback-only Django reproduction scripts that failed before a verdict.
Inspect the repository and return one corrected case for every supplied failed case. Preserve each finding_index. Fix the concrete exception without weakening the oracle or bypassing the changed business logic. Use django.utils.timezone.now() or another timezone-aware value whenever Django timezone handling is involved; never feed a naive datetime to timezone.localtime(). Keep all original safety restrictions: imports plus exactly reproduce(), no files, processes, network, external systems, commits, or transaction management. This is the only retry, so ensure fixture construction reaches the target code path.
Preserve each case's mode and predicted_changes. An assert-mode reproduce() must return exact comparable expected and observed values plus a non-empty cleanup_checks list; a differential-mode reproduce() returns observed and may omit expected and cleanup_checks. The fixed runner alone decides the verdict after tracing target execution. Do not return confirmed. Every stated precondition must be satisfied before calling the target business logic.

Failed cases:
{ReproductionPlan(plan.head_sha, failed_cases).to_json()}

Failure evidence:
{ReproductionEvidence(evidence.head_sha, failed_results).to_json()}
"""
    try:
        raw = run_codex_json(
            prompt, PLAN_SCHEMA, repository, codex_binary,
            fixed_fields={"version": 1, "head_sha": plan.head_sha},
            reasoning_effort=reasoning_effort, gitnexus_binary=gitnexus_binary,
        )
        repaired = ReproductionPlan.from_json(raw, 5)
    except (RuntimeError, ValueError) as exc:
        return ReproductionEvidence(evidence.head_sha, tuple(
            replace(item, observed=(
                f"{item.observed}; 자동 수정 실패: {type(exc).__name__}: {exc}"
            )[:1000]) if item.finding_index in failed_indexes else item
            for item in evidence.results
        ))
    originals = {case.finding_index: case for case in failed_cases}
    repaired = ReproductionPlan(plan.head_sha, tuple(
        replace(originals[case.finding_index], script=case.script)
        for case in repaired.cases if case.finding_index in failed_indexes
    ))
    if not repaired.cases:
        return ReproductionEvidence(evidence.head_sha, tuple(
            replace(item, observed=f"{item.observed}; 자동 수정 계획 없음"[:1000])
            if item.finding_index in failed_indexes else item for item in evidence.results
        ))
    retried = run_reproductions(repaired, repository, python, timeout, required_settings,
                                base_repository, differential_blocker)
    replacements = {
        item.finding_index: replace(item, observed=f"자동 수정 1회 후에도 실패: {item.observed}"[:1000])
        if item.status == "inconclusive" else item
        for item in retried.results
    }
    return ReproductionEvidence(evidence.head_sha, tuple(
        replacements.get(item.finding_index, item) for item in evidence.results
    ))


FORBIDDEN_IMPORTS = {"subprocess", "socket", "requests", "httpx", "urllib", "ftplib", "pathlib", "shutil", "os"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "commit", "set_autocommit",
                   "remove", "unlink", "rmdir", "rename", "write_text", "write_bytes", "mkdir"}


def validate_script(script: str) -> None:
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        raise ValueError("reproduction script is invalid Python") from exc
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1 or functions[0].name != "reproduce" or isinstance(functions[0], ast.AsyncFunctionDef):
        raise ValueError("reproduction script must define exactly reproduce()")
    if any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)) for node in tree.body):
        raise ValueError("reproduction script top level may contain only imports and reproduce()")
    function = functions[0]
    if function.decorator_list or function.args.args or function.args.kwonlyargs or function.args.vararg or function.args.kwarg:
        raise ValueError("reproduce() must be undecorated and accept no arguments")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] in FORBIDDEN_IMPORTS for alias in node.names):
            raise ValueError("reproduction script imports a forbidden module")
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
            raise ValueError("reproduction script imports a forbidden module")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in FORBIDDEN_CALLS:
                raise ValueError(f"reproduction script calls forbidden function: {name}")


CAPTURE_MODULE = "_gitea_auto_reviewer_capture"
DIFFERENTIAL_RUNS = 2


def run_reproductions(plan: ReproductionPlan, repository: Path, python: str, timeout: int,
                      required_settings: tuple[str, ...] = (), base_repository: Path | None = None,
                      differential_blocker: str | None = None) -> ReproductionEvidence:
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True).stdout.strip().lower()
    if actual != plan.head_sha:
        raise ValueError("reproduction checkout does not match the plan head SHA")
    if base_repository is None and differential_blocker is None:
        differential_blocker = "base 체크아웃이 제공되지 않음"
    results: list[ReproductionResult] = []
    with tempfile.TemporaryDirectory(prefix="gitea-reproduce-") as directory:
        root = Path(directory)
        runner = root / "runner.py"
        runner.write_text(_RUNNER_SOURCE, encoding="utf-8")
        (root / f"{CAPTURE_MODULE}.py").write_text(
            Path(__file__).with_name("_capture.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (root / "home").mkdir()
        environment = _reproduction_environment(root / "home", repository)
        for position, case in enumerate(plan.cases):
            case_path = root / f"case-{position}.py"
            case_path.write_text(case.script, encoding="utf-8")
            started = time.monotonic()

            def execute(checkout: Path, name: str, capture: bool, require_target: bool) -> dict[str, Any]:
                return _execute_case(python, runner, case_path, root / f"result-{position}-{name}.json",
                                     checkout, environment, timeout, required_settings, case.target_evidence,
                                     {"capture": capture, "capture_dir": str(root),
                                      "capture_module": CAPTURE_MODULE, "require_target": require_target})

            if case.mode == "differential":
                results.append(_differential_result(case, execute, repository, base_repository,
                                                    differential_blocker, started))
                continue
            payload = execute(repository, "head", False, True)
            status = payload["status"]
            if payload.get("cleanup_verified") is not True:
                status = "inconclusive"
            results.append(ReproductionResult(
                case.finding_index, status, case.condition, case.oracle,
                str(payload.get("expected", case.oracle))[:1000], str(payload.get("observed", ""))[:1000],
                payload.get("cleanup_verified") is True, round(time.monotonic() - started, 3),
                *_population(payload), payload.get("target_reached") is True,
                tuple(payload.get("reached_targets", ())), case.script,
            ))
    return _complete_evidence(plan, results)


def _execute_case(python: str, runner: Path, case_path: Path, output_path: Path, checkout: Path,
                  environment: dict[str, str], timeout: int, required_settings: tuple[str, ...],
                  targets: tuple[str, ...], options: dict[str, Any]) -> dict[str, Any]:
    """Run one isolated runner process and normalize every failure into an inconclusive payload."""
    try:
        process = subprocess.run([python, str(runner), str(case_path), str(output_path),
                                  json.dumps(required_settings), json.dumps(targets), json.dumps(options)],
                                 cwd=checkout, env=environment, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout, check=False)
        payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"status": "inconclusive", "observed": type(exc).__name__, "cleanup_verified": False}
    if not isinstance(payload, dict):
        payload = {}
    if process.returncode != 0 or "status" not in payload:
        payload["status"] = "inconclusive"
        payload.setdefault("observed", (process.stderr or "execution failed")[-1000:])
    return payload


def _differential_result(case: ReproductionCase, execute, repository: Path, base_repository: Path | None,
                         differential_blocker: str | None, started: float) -> ReproductionResult:
    """Run the scenario twice on head and base, then let the fixed comparator decide."""
    def result(status: str, observed: str, cleanup: bool, head: dict[str, Any] | None = None,
               predicted: tuple[str, ...] = (), other: tuple[str, ...] = ()) -> ReproductionResult:
        head = head or {}
        return ReproductionResult(
            case.finding_index, status, case.condition, case.oracle, "base 실행 결과와 동일", observed[:1000],
            cleanup, round(time.monotonic() - started, 3), *_population(head),
            head.get("target_reached") is True, tuple(head.get("reached_targets", ())), case.script,
            "differential", predicted, other,
        )

    if differential_blocker is not None or base_repository is None:
        return result("inconclusive", f"base·head 차분 실행 불가: {differential_blocker}", False)
    heads = [execute(repository, f"head-{run}", True, True) for run in range(DIFFERENTIAL_RUNS)]
    failed = next((item for item in heads if item["status"] != "captured"), None)
    if failed is not None:
        return result("inconclusive", f"head 실행 실패: {failed.get('observed', '')}", False, failed)
    bases = [execute(base_repository, f"base-{run}", True, False) for run in range(DIFFERENTIAL_RUNS)]
    failed = next((item for item in bases if item["status"] != "captured"), None)
    if failed is not None:
        return result("inconclusive", f"base 실행 실패: {failed.get('observed', '')}", False, heads[0])
    cleanup = all(item.get("cleanup_verified") is True for item in (*heads, *bases))
    if not cleanup:
        return result("inconclusive", "롤백 후 DB 복원을 확인하지 못함", False, heads[0])
    comparison = compare_runs([item["outcome"] for item in bases], [item["outcome"] for item in heads],
                              case.predicted_changes)
    return result(comparison.status, comparison.summary, True, heads[0],
                  comparison.predicted, comparison.unpredicted)


def _population(payload: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    label, matching, total = (payload.get("population_label"), payload.get("matching_count"),
                              payload.get("total_count"))
    if (not isinstance(label, str) or not label.strip() or len(label.strip()) > 200
            or type(matching) is not int or type(total) is not int
            or total <= 0 or matching < 0 or matching > total):
        return None, None, None
    return label.strip(), matching, total


def _reproduction_environment(home: Path, repository: Path) -> dict[str, str]:
    environment = safe_evidence_environment(home)
    if not environment.get("DJANGO_SETTINGS_MODULE"):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(repository / "pytest.ini", encoding="utf-8")
        module = parser.get("pytest", "DJANGO_SETTINGS_MODULE", fallback="").strip()
        if module:
            environment["DJANGO_SETTINGS_MODULE"] = module
    return environment


def _complete_evidence(plan: ReproductionPlan, results: list[ReproductionResult]) -> ReproductionEvidence:
    if len(results) != len(plan.cases):
        raise RuntimeError("reproduction result count does not match the plan")
    return ReproductionEvidence(plan.head_sha, tuple(results))


def finalize_review(review: Review, evidence: ReproductionEvidence,
                    decision: VerificationDecision | None = None) -> Review:
    accepted = (set(decision.accepted_finding_indices) if decision is not None else
                {item.finding_index for item in evidence.results
                 if item.status == "confirmed" and item.cleanup_verified and item.target_reached})
    confirmed: list[ReproducedFinding] = []
    confirmed_indexes: set[int] = set()
    for result in evidence.results:
        if (result.status != "confirmed" or not result.cleanup_verified or not result.target_reached
                or result.finding_index not in accepted):
            continue
        try:
            finding = review.findings[result.finding_index]
        except IndexError as exc:
            raise ValueError("reproduction references a missing finding") from exc
        confirmed.append(ReproducedFinding(finding.problem, finding.impact, finding.evidence,
                                           result.condition, result.oracle, result.expected,
                                           result.observed, True, result.population_label,
                                           result.matching_count, result.total_count,
                                           result.reached_targets, result.mode,
                                           result.predicted_differences, result.other_differences))
        confirmed_indexes.add(result.finding_index)
    results = {item.finding_index: item for item in evidence.results}
    rejection_reasons = ({item.finding_index: item.reason for item in decision.rejected_findings}
                         if decision is not None else {})
    static_findings = []
    for index, finding in enumerate(review.findings):
        if index in confirmed_indexes:
            continue
        result = results.get(index)
        if result is None:
            status, detail = "unplanned", "현재 Django/ORM 롤백 재현 범위에서 계획되지 않음"
        elif result.status == "confirmed" and not result.target_reached:
            status, detail = "inconclusive", "변경 근거 코드 도달이 확인되지 않음"
        elif result.status == "confirmed":
            status = "verification_rejected"
            detail = rejection_reasons.get(index, "2차 검증 미채택 사유가 기록되지 않음")
        elif result.status == "refuted":
            status, detail = "not_reproduced", result.observed or "조건 실행에서 문제를 관찰하지 못함"
        else:
            status, detail = "inconclusive", result.observed or "실행 결과를 판정하지 못함"
        static_findings.append(replace(finding, reproduction_status=status,
                                       reproduction_detail=detail[:1000]))
    if confirmed or static_findings:
        return replace(review, findings=tuple(static_findings), reproduced_findings=tuple(confirmed))
    return replace(review, findings=(), reproduced_findings=(), risk="low",
                   risk_confidence="high", risk_evidence=())


_RUNNER_SOURCE = r'''import importlib, importlib.util, json, sys
from contextlib import ExitStack
from pathlib import Path

def write(value):
    Path(sys.argv[2]).write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")

try:
    sys.path.insert(0, str(Path.cwd()))
    options = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
    capture = options.get("capture") is True
    import django
    django.setup()
    from django.apps import apps
    from django.conf import settings
    from django.db import connections, transaction
    for requirement in json.loads(sys.argv[3]):
        name, expected = requirement.split("=", 1)
        actual = str(getattr(settings, name, None)).lower()
        if actual != expected.lower():
            raise RuntimeError(f"required Django setting {name}={expected}, got {actual}")
    recorder = None
    if capture:
        sys.path.insert(1, options["capture_dir"])
        capture_module = importlib.import_module(options["capture_module"])
        recorder = capture_module.ChangeRecorder(connections, apps)
    spec = importlib.util.spec_from_file_location("reproduction_case", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = Path.cwd().resolve()
    targets = {}
    for reference in json.loads(sys.argv[4]):
        path, line = reference.rsplit(":", 1)
        targets.setdefault(path.replace("\\", "/"), set()).add(int(line))
    reached = set()
    def trace(frame, event, arg):
        if event != "line":
            return trace
        try:
            path = Path(frame.f_code.co_filename).resolve().relative_to(repository).as_posix()
        except ValueError:
            return trace
        for target_line in targets.get(path, ()):
            if abs(frame.f_lineno - target_line) <= 3:
                reached.add(f"{path}:{target_line}")
        return trace
    aliases = list(connections)
    scenario_error, changes = None, None
    with ExitStack() as stack:
        for alias in aliases:
            stack.enter_context(transaction.atomic(using=alias))
        if recorder is not None:
            recorder.install(stack)
        sys.settrace(trace)
        try:
            if recorder is None:
                result = module.reproduce()
            else:
                # A savepoint keeps the outer transaction usable when the target raises.
                try:
                    with ExitStack() as scenario:
                        for alias in aliases:
                            scenario.enter_context(transaction.atomic(using=alias))
                        result = module.reproduce()
                except Exception as exc:
                    scenario_error, result = f"{type(exc).__name__}: {exc}"[:500], {}
        finally:
            sys.settrace(None)
        if not isinstance(result, dict):
            raise TypeError("reproduce() must return a dict")
        if recorder is not None:
            changes = recorder.collect()
        for alias in aliases:
            transaction.set_rollback(True, using=alias)
    connections.close_all()
    checks = result.get("cleanup_checks", [])
    cleanup = bool(checks) or recorder is not None
    for check in checks:
        query = apps.get_model(check["model"]).objects.filter(**check["lookup"])
        if "exists" in check:
            cleanup = cleanup and query.exists() is check["exists"]
        else:
            row = query.get()
            cleanup = cleanup and str(getattr(row, check["field"])) == str(check["equals"])
    if recorder is not None:
        cleanup = cleanup and recorder.restored()
    target_reached = bool(reached)
    population = {"population_label": result.get("population_label"),
                  "matching_count": result.get("matching_count"), "total_count": result.get("total_count")}
    if recorder is not None:
        status, observed = "captured", "captured"
        if options.get("require_target") and not target_reached:
            status, observed = "inconclusive", "변경 근거 코드에 도달하지 못함: " + ", ".join(sorted(targets))
        write({"status": status, "observed": observed, "cleanup_verified": cleanup,
               "target_reached": target_reached, "reached_targets": sorted(reached),
               "outcome": {"result": capture_module.json_value(result.get("observed")),
                           "exception": scenario_error, "changes": changes},
               **population})
        sys.exit(0)
    has_values = "expected" in result and "observed" in result
    if not targets:
        status, observed = "inconclusive", "실행 가능한 Python target evidence가 없음"
    elif not target_reached:
        status, observed = "inconclusive", "변경 근거 코드에 도달하지 못함: " + ", ".join(sorted(targets))
    elif not has_values:
        status, observed = "inconclusive", "expected/observed 관찰값이 없음"
    else:
        status, observed = ("confirmed" if result["expected"] != result["observed"] else "refuted",
                            str(result["observed"]))
    write({"status": status,
           "expected": str(result.get("expected", "")), "observed": observed,
           "cleanup_verified": cleanup,
           "target_reached": target_reached, "reached_targets": sorted(reached),
           **population})
except Exception as exc:
    write({"status": "inconclusive", "expected": "", "observed": f"{type(exc).__name__}: {exc}",
           "cleanup_verified": False, "target_reached": False, "reached_targets": []})
    raise
'''
