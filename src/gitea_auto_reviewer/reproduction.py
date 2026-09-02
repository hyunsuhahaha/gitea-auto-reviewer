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
            "required": ["finding_index", "condition", "oracle", "script"],
            "properties": {
                "finding_index": {"type": "integer", "minimum": 0, "maximum": 4},
                "condition": {"type": "string", "minLength": 1, "maxLength": 1000},
                "oracle": {"type": "string", "minLength": 1, "maxLength": 1000},
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
            if not isinstance(item, dict) or not required <= set(item) <= required | {"target_evidence"}:
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
            parsed.append(ReproductionCase(
                index, condition.strip(), oracle.strip(), script, tuple(normalized_targets)
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


@dataclass(frozen=True)
class ReproductionEvidence:
    head_sha: str
    results: tuple[ReproductionResult, ...]

    @classmethod
    def from_json(cls, raw: str) -> "ReproductionEvidence":
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "results"} or value["version"] != 1:
            raise ValueError("invalid reproduction evidence")
        results = tuple(ReproductionResult(
            **{**item, "reached_targets": tuple(item.get("reached_targets", ()))},
        ) for item in value["results"])
        if any(item.status not in {"confirmed", "refuted", "inconclusive"} for item in results):
            raise ValueError("invalid reproduction status")
        if any(type(item.target_reached) is not bool or any(
                not isinstance(ref, str) for ref in item.reached_targets) for item in results):
            raise ValueError("invalid reproduction target evidence")
        if any(not isinstance(item.script, str) or len(item.script) > 50000 for item in results):
            raise ValueError("invalid reproduction script evidence")
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


def build_plan_prompt(review: Review, head_sha: str) -> str:
    return f"""You are planning rollback-only reproductions for candidate code-review findings.
The repository is already checked out at PR head {head_sha}. Inspect it, including GitNexus MCP context.

Return one case for every finding that can be objectively reproduced by importing Django and directly calling ORM/service/view code against the configured test database. Skip subjective, destructive, external-network, browser-only, or schema-incompatible cases.
`finding_index` is the zero-based position in the candidate review's `findings` array. Return at most one case for each finding and never invent an index outside that array.

Write user-visible explanations such as `condition` and `oracle` in Korean. Return `expected` and `observed` as exact JSON-compatible business values rather than explanatory prose; keep code identifiers and concrete values unchanged. Write `condition` as 1-6 concise, unnumbered lines describing the minimal generalized data state and final action required for the bug; never combine them into a paragraph. Do not present arbitrary fixture values chosen by the reproduction script as required conditions. Omit exact quantities, IDs, dates, and ratios unless that exact value or boundary is causally required for the bug. Put chosen example values and calculations only in `observed`.

Each script must contain only imports plus exactly `def reproduce():`, and return a JSON-compatible dict:
  expected: the exact JSON-compatible value required by the oracle
  observed: the exact JSON-compatible value produced by the target code
  cleanup_checks: non-empty list of {{model, lookup, field, equals}} or {{model, lookup, exists}}
  population_label, matching_count, total_count: prevalence data counted from untouched test-DB rows before any reproduction mutation. Return all three whenever ORM can define a defensible natural population and matching condition; omit all three only when it cannot. Never invent counts. These values are informational and never decide whether a finding is confirmed.

The fixed runner, not the script, decides confirmed/refuted by comparing expected and observed after proving that cited target code executed. Never return or calculate a confirmed verdict. The fixed runner supplies django.setup(), transaction.atomic(), forced rollback, a fresh-connection cleanup check, code-reach tracing, and exception handling. Do not manage transactions. Select existing records semantically through ORM; never hard-code database primary keys. Do not write files, spawn processes, use network clients, call ERP, or mutate anything outside the rollback transaction. RequestFactory/SimpleNamespace and direct Django view calls are allowed.
Use timezone-aware datetimes compatible with the repository settings. Prefer django.utils.timezone.now(); never pass a naive datetime to timezone.localtime() or timezone-aware model logic. The script must reach the candidate's changed business logic rather than failing during fixture construction.

Candidate review JSON:
{review.to_json()}
"""


def plan_reproductions(review: Review, head_sha: str, repository: Path, codex_binary: str,
                       gitnexus_binary: str, reasoning_effort: str = "medium") -> ReproductionPlan:
    if not review.findings:
        return ReproductionPlan(validate_sha(head_sha), ())
    raw = run_codex_json(build_plan_prompt(review, head_sha), PLAN_SCHEMA, repository, codex_binary,
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
) -> ReproductionEvidence:
    failed_indexes = {item.finding_index for item in evidence.results if item.status == "inconclusive"}
    if not failed_indexes:
        return evidence
    failed_cases = tuple(case for case in plan.cases if case.finding_index in failed_indexes)
    failed_results = tuple(item for item in evidence.results if item.finding_index in failed_indexes)
    prompt = f"""You are repairing rollback-only Django reproduction scripts that failed before a verdict.
Inspect the repository and return one corrected case for every supplied failed case. Preserve each finding_index. Fix the concrete exception without weakening the oracle or bypassing the changed business logic. Use django.utils.timezone.now() or another timezone-aware value whenever Django timezone handling is involved; never feed a naive datetime to timezone.localtime(). Keep all original safety restrictions: imports plus exactly reproduce(), no files, processes, network, external systems, commits, or transaction management. This is the only retry, so ensure fixture construction reaches the target code path.
Each reproduce() must return exact comparable expected and observed values plus a non-empty cleanup_checks list. The fixed runner alone decides the verdict after tracing target execution. Do not return confirmed. Every stated precondition must be satisfied before calling the target business logic.

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
        ReproductionCase(case.finding_index, originals[case.finding_index].condition,
                         originals[case.finding_index].oracle, case.script,
                         originals[case.finding_index].target_evidence)
        for case in repaired.cases if case.finding_index in failed_indexes
    ))
    if not repaired.cases:
        return ReproductionEvidence(evidence.head_sha, tuple(
            replace(item, observed=f"{item.observed}; 자동 수정 계획 없음"[:1000])
            if item.finding_index in failed_indexes else item for item in evidence.results
        ))
    retried = run_reproductions(repaired, repository, python, timeout, required_settings)
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


def run_reproductions(plan: ReproductionPlan, repository: Path, python: str, timeout: int,
                      required_settings: tuple[str, ...] = ()) -> ReproductionEvidence:
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True).stdout.strip().lower()
    if actual != plan.head_sha:
        raise ValueError("reproduction checkout does not match the plan head SHA")
    results: list[ReproductionResult] = []
    with tempfile.TemporaryDirectory(prefix="gitea-reproduce-") as directory:
        root = Path(directory)
        runner = root / "runner.py"
        runner.write_text(_RUNNER_SOURCE, encoding="utf-8")
        (root / "home").mkdir()
        environment = _reproduction_environment(root / "home", repository)
        for position, case in enumerate(plan.cases):
            case_path, output_path = root / f"case-{position}.py", root / f"result-{position}.json"
            case_path.write_text(case.script, encoding="utf-8")
            started = time.monotonic()
            try:
                process = subprocess.run([python, str(runner), str(case_path), str(output_path),
                                          json.dumps(required_settings), json.dumps(case.target_evidence)],
                                         cwd=repository, env=environment, capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", timeout=timeout, check=False)
                payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
                status = payload.get("status", "inconclusive") if process.returncode == 0 else "inconclusive"
                expected = str(payload.get("expected", case.oracle))[:1000]
                observed = str(payload.get("observed", (process.stderr or "execution failed")[-1000:]))[:1000]
                cleanup = payload.get("cleanup_verified") is True
                target_reached = payload.get("target_reached") is True
                reached_targets = tuple(payload.get("reached_targets", ()))
                population = _population(payload)
                if not cleanup:
                    status = "inconclusive"
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                status, expected, observed, cleanup = "inconclusive", case.oracle, type(exc).__name__, False
                target_reached, reached_targets = False, ()
                population = (None, None, None)
            results.append(ReproductionResult(case.finding_index, status, case.condition, case.oracle,
                                              expected, observed, cleanup, round(time.monotonic() - started, 3),
                                              *population, target_reached, reached_targets, case.script))
    return _complete_evidence(plan, results)


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
                                           result.reached_targets))
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


_RUNNER_SOURCE = r'''import importlib.util, json, sys
from contextlib import ExitStack
from pathlib import Path

def write(value):
    Path(sys.argv[2]).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

try:
    sys.path.insert(0, str(Path.cwd()))
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
    with ExitStack() as stack:
        for alias in aliases:
            stack.enter_context(transaction.atomic(using=alias))
        sys.settrace(trace)
        try:
            result = module.reproduce()
        finally:
            sys.settrace(None)
        for alias in aliases:
            transaction.set_rollback(True, using=alias)
    connections.close_all()
    checks = result.get("cleanup_checks", [])
    cleanup = bool(checks)
    for check in checks:
        query = apps.get_model(check["model"]).objects.filter(**check["lookup"])
        if "exists" in check:
            cleanup = cleanup and query.exists() is check["exists"]
        else:
            row = query.get()
            cleanup = cleanup and str(getattr(row, check["field"])) == str(check["equals"])
    has_values = "expected" in result and "observed" in result
    target_reached = bool(reached)
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
           "population_label": result.get("population_label"),
           "matching_count": result.get("matching_count"), "total_count": result.get("total_count")})
except Exception as exc:
    write({"status": "inconclusive", "expected": "", "observed": f"{type(exc).__name__}: {exc}",
           "cleanup_verified": False, "target_reached": False, "reached_targets": []})
    raise
'''
