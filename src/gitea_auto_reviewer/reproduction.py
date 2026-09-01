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
    "required": ["version", "head_sha", "accepted_finding_indices"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "head_sha": {"type": "string"},
        "accepted_finding_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 4},
        },
    },
}


@dataclass(frozen=True)
class ReproductionCase:
    finding_index: int
    condition: str
    oracle: str
    script: str


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
            if not isinstance(item, dict) or set(item) != {"finding_index", "condition", "oracle", "script"}:
                raise ValueError("invalid reproduction case")
            index = item["finding_index"]
            if type(index) is not int or not 0 <= index < finding_count or index in indexes:
                continue
            condition, oracle, script = item["condition"], item["oracle"], item["script"]
            if not all(isinstance(text, str) and text.strip() for text in (condition, oracle, script)):
                raise ValueError("reproduction case text must not be empty")
            validate_script(script)
            parsed.append(ReproductionCase(index, condition.strip(), oracle.strip(), script))
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


@dataclass(frozen=True)
class ReproductionEvidence:
    head_sha: str
    results: tuple[ReproductionResult, ...]

    @classmethod
    def from_json(cls, raw: str) -> "ReproductionEvidence":
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "results"} or value["version"] != 1:
            raise ValueError("invalid reproduction evidence")
        results = tuple(ReproductionResult(**item) for item in value["results"])
        if any(item.status not in {"confirmed", "refuted", "inconclusive"} for item in results):
            raise ValueError("invalid reproduction status")
        return cls(validate_sha(value["head_sha"]), results)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "head_sha": self.head_sha, "results": [asdict(item) for item in self.results]}, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class VerificationDecision:
    head_sha: str
    accepted_finding_indices: tuple[int, ...]

    @classmethod
    def from_json(cls, raw: str, evidence: ReproductionEvidence) -> "VerificationDecision":
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "accepted_finding_indices"} or value["version"] != 1:
            raise ValueError("invalid reproduction verification")
        indexes = value["accepted_finding_indices"]
        confirmed = {item.finding_index for item in evidence.results
                     if item.status == "confirmed" and item.cleanup_verified}
        if (not isinstance(indexes, list) or any(type(index) is not int for index in indexes)
                or len(indexes) != len(set(indexes)) or not set(indexes) <= confirmed):
            raise ValueError("verification may accept only confirmed findings")
        head_sha = validate_sha(value["head_sha"])
        if head_sha != evidence.head_sha:
            raise ValueError("verification belongs to a different PR head SHA")
        return cls(head_sha, tuple(indexes))

    def to_json(self) -> str:
        return json.dumps({"version": 1, "head_sha": self.head_sha,
                           "accepted_finding_indices": list(self.accepted_finding_indices)}, indent=2)


def build_plan_prompt(review: Review, head_sha: str) -> str:
    return f"""You are planning rollback-only reproductions for candidate code-review findings.
The repository is already checked out at PR head {head_sha}. Inspect it, including GitNexus MCP context.

Return one case for every finding that can be objectively reproduced by importing Django and directly calling ORM/service/view code against the configured test database. Skip subjective, destructive, external-network, browser-only, or schema-incompatible cases.
`finding_index` is the zero-based position in the candidate review's `findings` array. Return at most one case for each finding and never invent an index outside that array.

Write every user-visible explanation in Korean. In particular, `condition`, `oracle`, and the script's returned `expected` and `observed` strings must be Korean. Keep code identifiers and concrete values unchanged when needed. Write `condition` as 1-6 concise, unnumbered lines describing the minimal generalized data state and final action required for the bug; never combine them into a paragraph. Do not present arbitrary fixture values chosen by the reproduction script as required conditions. Omit exact quantities, IDs, dates, and ratios unless that exact value or boundary is causally required for the bug. Put chosen example values and calculations only in `observed`.

Each script must contain only imports plus exactly `def reproduce():`, and return a JSON-compatible dict:
  confirmed: boolean (true only when the stated bad behavior was actually observed)
  expected: short string
  observed: short string containing concrete values or response text
  cleanup_checks: non-empty list of {{model, lookup, field, equals}} or {{model, lookup, exists}}
  population_label, matching_count, total_count: prevalence data counted from untouched test-DB rows before any reproduction mutation. Return all three whenever ORM can define a defensible natural population and matching condition; omit all three only when it cannot. Never invent counts. These values are informational and never decide whether a finding is confirmed.

The fixed runner supplies django.setup(), transaction.atomic(), forced rollback, a fresh-connection cleanup check, and exception handling. Do not manage transactions. Select existing records semantically through ORM; never hard-code database primary keys. Do not write files, spawn processes, use network clients, call ERP, or mutate anything outside the rollback transaction. RequestFactory/SimpleNamespace and direct Django view calls are allowed.

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
    return ReproductionPlan.from_json(raw, len(review.findings))


def verify_reproductions(review: Review, evidence: ReproductionEvidence, repository: Path,
                         codex_binary: str, gitnexus_binary: str,
                         reasoning_effort: str = "low") -> VerificationDecision:
    confirmed = [item for item in evidence.results if item.status == "confirmed" and item.cleanup_verified]
    if not confirmed:
        return VerificationDecision(evidence.head_sha, ())
    prompt = f"""You are the second, adversarial verification pass for code-review findings.
Only the candidate findings listed in the reproduction evidence were executed against the test database.
For each confirmed result, inspect the repository and GitNexus again and try to disprove that the observed result supports the exact candidate problem and impact. Accept an index only when the oracle is objective, the observation demonstrates that exact problem, cleanup was verified, and no concrete code path invalidates the conclusion. Never add, rewrite, or accept an unconfirmed finding. Silence is valid.

Candidate review:
{review.to_json()}

Reproduction evidence:
{evidence.to_json()}
"""
    raw = run_codex_json(prompt, VERIFICATION_SCHEMA, repository, codex_binary,
                         fixed_fields={"version": 1, "head_sha": evidence.head_sha},
                         reasoning_effort=reasoning_effort, gitnexus_binary=gitnexus_binary)
    return VerificationDecision.from_json(raw, evidence)


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
                process = subprocess.run([python, str(runner), str(case_path), str(output_path), json.dumps(required_settings)],
                                         cwd=repository, env=environment, capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", timeout=timeout, check=False)
                payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
                status = payload.get("status", "inconclusive") if process.returncode == 0 else "inconclusive"
                expected = str(payload.get("expected", case.oracle))[:1000]
                observed = str(payload.get("observed", (process.stderr or "execution failed")[-1000:]))[:1000]
                cleanup = payload.get("cleanup_verified") is True
                population = _population(payload)
                if not cleanup:
                    status = "inconclusive"
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                status, expected, observed, cleanup = "inconclusive", case.oracle, type(exc).__name__, False
                population = (None, None, None)
            results.append(ReproductionResult(case.finding_index, status, case.condition, case.oracle,
                                              expected, observed, cleanup, round(time.monotonic() - started, 3),
                                              *population))
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
                 if item.status == "confirmed" and item.cleanup_verified})
    confirmed: list[ReproducedFinding] = []
    confirmed_indexes: set[int] = set()
    for result in evidence.results:
        if result.status != "confirmed" or not result.cleanup_verified or result.finding_index not in accepted:
            continue
        try:
            finding = review.findings[result.finding_index]
        except IndexError as exc:
            raise ValueError("reproduction references a missing finding") from exc
        confirmed.append(ReproducedFinding(finding.problem, finding.impact, finding.evidence,
                                           result.condition, result.oracle, result.expected,
                                           result.observed, True, result.population_label,
                                           result.matching_count, result.total_count))
        confirmed_indexes.add(result.finding_index)
    refuted_indexes = {item.finding_index for item in evidence.results if item.status == "refuted"}
    results = {item.finding_index: item for item in evidence.results}
    static_findings = []
    for index, finding in enumerate(review.findings):
        if index in confirmed_indexes or index in refuted_indexes:
            continue
        result = results.get(index)
        if result is None:
            status, detail = "unplanned", "현재 Django/ORM 롤백 재현 범위에서 계획되지 않음"
        elif result.status == "confirmed":
            status, detail = "verification_rejected", "재현 결과가 문제와 영향을 입증하기에 불충분함"
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
    aliases = list(connections)
    with ExitStack() as stack:
        for alias in aliases:
            stack.enter_context(transaction.atomic(using=alias))
        result = module.reproduce()
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
    write({"status": "confirmed" if result.get("confirmed") else "refuted",
           "expected": str(result.get("expected", "")), "observed": str(result.get("observed", "")),
           "cleanup_verified": cleanup,
           "population_label": result.get("population_label"),
           "matching_count": result.get("matching_count"), "total_count": result.get("total_count")})
except Exception as exc:
    write({"status": "inconclusive", "expected": "", "observed": f"{type(exc).__name__}: {exc}",
           "cleanup_verified": False})
    raise
'''
