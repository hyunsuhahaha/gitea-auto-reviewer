"""Deterministic evidence collected by an unprivileged CI job."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import MAX_TEST_SHARDS
from .git_context import validate_sha
from .review import TestScope
from .selection import Selection, TestMap

STATUSES = {"pass", "fail", "error", "not_run"}
TRACE_PLUGIN = "_gitea_auto_reviewer_trace"
NO_TESTS_COLLECTED = 5
MAX_TRACE_CHAINS = 20
MAX_TRACE_TESTS = 20
SAFE_ENVIRONMENT_NAMES = {
    "CI", "COMSPEC", "DJANGO_SETTINGS_MODULE", "LANG", "LC_ALL",
    "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PYTHONIOENCODING", "PYTHONPATH", "PYTHONUTF8", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
}


@dataclass(frozen=True)
class EnvironmentPolicy:
    """Project-specific additions to the PR-code environment, taken from the base config."""

    pass_env: tuple[str, ...] = ()
    django_settings_module: str | None = None


@dataclass(frozen=True)
class Check:
    status: str
    summary: str
    passed: int | None = None
    total: int | None = None

    @classmethod
    def from_value(cls, value: object) -> Check:
        if not isinstance(value, dict) or set(value) != {"status", "summary", "passed", "total"}:
            raise ValueError("invalid evidence check")
        status, summary = value["status"], value["summary"]
        passed, total = value["passed"], value["total"]
        if not isinstance(status, str) or status not in STATUSES or not isinstance(summary, str) or len(summary) > 2000:
            raise ValueError("invalid evidence check")
        if passed is not None and (not isinstance(passed, int) or isinstance(passed, bool) or passed < 0):
            raise ValueError("invalid evidence count")
        if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
            raise ValueError("invalid evidence count")
        if (passed is None) != (total is None) or (passed is not None and passed > total):
            raise ValueError("invalid evidence count")
        return cls(status, summary, passed, total)


@dataclass(frozen=True)
class Evidence:
    head_sha: str
    django_check: Check
    migration_check: Check
    pytest: Check
    test_scope: TestScope | None = None

    @classmethod
    def from_json(cls, raw: str) -> Evidence:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("evidence is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) - {"test_scope"} != {"version", "head_sha", "checks"}:
            raise ValueError("invalid evidence document")
        checks = value["checks"]
        if type(value["version"]) is not int or value["version"] != 1 or not isinstance(checks, dict) or set(checks) != {
            "django_check", "migration_check", "pytest"
        }:
            raise ValueError("invalid evidence document")
        return cls(
            validate_sha(value["head_sha"]),
            Check.from_value(checks["django_check"]),
            Check.from_value(checks["migration_check"]),
            Check.from_value(checks["pytest"]),
            None if value.get("test_scope") is None else TestScope.from_value(value["test_scope"]),
        )

    def to_json(self) -> str:
        value: dict = {"version": 1, "head_sha": self.head_sha, "checks": {
            "django_check": asdict(self.django_check),
            "migration_check": asdict(self.migration_check),
            "pytest": asdict(self.pytest),
        }}
        if self.test_scope is not None:
            value["test_scope"] = asdict(self.test_scope)
        return json.dumps(value, ensure_ascii=False, indent=2)


def collect_evidence(repository: Path, head_sha: str, python: str = "python", timeout: int = 900,
                     only: str | None = None, base_sha: str | None = None,
                     trace_output: Path | None = None,
                     policy: EnvironmentPolicy = EnvironmentPolicy(),
                     selection: Selection | None = None, record_map: Path | None = None,
                     shards: int = 1) -> Evidence:
    head_sha = validate_sha(head_sha)
    if only not in {None, "django_check", "migration_check", "pytest"}:
        raise ValueError("unknown evidence check")
    if trace_output is not None and not base_sha:
        raise ValueError("runtime call tracing requires the base SHA")
    if selection is not None and selection.head_sha != head_sha:
        raise ValueError("test selection belongs to a different PR head SHA")
    if type(shards) is not int or not 1 <= shards <= MAX_TEST_SHARDS:
        raise ValueError(f"shards must be an integer between 1 and {MAX_TEST_SHARDS}")
    with tempfile.TemporaryDirectory(prefix="gitea-evidence-") as home:
        environment = safe_evidence_environment(Path(home), policy)
        try:
            actual_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=True, env=environment,
            ).stdout.strip().lower()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("could not verify evidence checkout HEAD") from exc
        if actual_head != head_sha:
            raise ValueError("evidence checkout does not match the supplied PR head SHA")
        if not (repository / "manage.py").is_file():
            raise ValueError("manage.py was not found in the repository root")
        not_run = Check("not_run", "not selected")
        migration = not_run
        if only in {None, "migration_check"}:
            if base_sha and not _python_changed(repository, validate_sha(base_sha), head_sha, environment):
                migration = Check("pass", "Skipped: no Python files changed")
            else:
                migration = _run_migration(
                    [python, "manage.py", "makemigrations", "--check", "--dry-run", "--noinput"],
                    repository, timeout, environment,
                )
        pytest_check, scope = not_run, None
        if only in {None, "pytest"}:
            ranges = None
            if trace_output is not None:
                from .impact import changed_ranges

                ranges = changed_ranges(repository, validate_sha(base_sha), head_sha)
            pytest_check, scope = _run_test_suite(
                [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], repository, timeout, environment,
                Path(home), head_sha, ranges, trace_output, record_map, selection, shards,
            )
        return Evidence(head_sha,
            _run([python, "manage.py", "check"], repository, timeout, environment)
            if only in {None, "django_check"} else not_run,
            migration,
            pytest_check,
            scope,
        )


def _run_test_suite(command: list[str], repository: Path, timeout: int, environment: dict[str, str],
                    home: Path, head_sha: str, ranges: dict | None, trace_output: Path | None,
                    record_map: Path | None, selection: Selection | None,
                    shards: int) -> tuple[Check, TestScope | None]:
    """Run pytest once, or as duration-balanced shards in parallel, with the in-process plugin."""
    if ranges is None and record_map is None and selection is None and shards == 1:
        return _run_pytest(command, repository, timeout, environment), None
    plugin_dir = home / "trace-plugin"
    plugin_dir.mkdir()
    (plugin_dir / f"{TRACE_PLUGIN}.py").write_text(
        Path(__file__).with_name("_trace_plugin.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    selecting = selection is not None or shards > 1
    runs = []
    for index in range(shards):
        directory = home / f"shard-{index}"
        directory.mkdir()
        config: dict = {"repository": str(repository.resolve())}
        if ranges is not None:
            config["trace"] = {"changed": ranges, "output": str(directory / "trace.json")}
        if record_map is not None:
            config["record"] = {"output": str(directory / "record.json")}
        if selecting:
            config["select"] = _select_config(selection, index, shards, directory / "selected.json")
        (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
        shard_environment = dict(environment)
        shard_environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(plugin_dir), environment.get("PYTHONPATH", "")) if item
        )
        shard_environment["GITEA_AUTO_REVIEWER_TRACE"] = str(directory / "config.json")
        if shards > 1:
            # Lets the project's CI settings give every shard its own test database name.
            shard_environment["AI_REVIEW_SHARD_INDEX"] = str(index)
            shard_environment["AI_REVIEW_SHARD_COUNT"] = str(shards)
        runs.append(shard_environment)
    with ThreadPoolExecutor(max_workers=shards) as pool:
        checks = list(pool.map(
            lambda run: _run_pytest(command + ["-p", TRACE_PLUGIN], repository, timeout, run, selecting), runs,
        ))
    directories = [home / f"shard-{index}" for index in range(shards)]
    if trace_output is not None:
        trace_output.write_text(json.dumps(_merge_traces(directories), ensure_ascii=False, indent=2),
                                encoding="utf-8")
    if record_map is not None:
        _write_test_map(directories, head_sha, record_map)
    reports = [_read_json(directory / "selected.json") for directory in directories]
    if shards > 1:
        for index, report in enumerate(reports):
            if report:
                print(f"Shard {index + 1}/{shards}: {report['shard_tests']} tests, "
                      f"~{report['shard_seconds']:.1f}s by recorded durations", flush=True)
    check = _combine_checks(checks)
    if selection is None or not reports[0]:
        return check, None
    collected, selected = reports[0]["collected"], reports[0]["selected"]
    estimate, total = selection.estimated_seconds
    print(f"Test selection ({selection.mode}): ran {selected} of {collected} tests, "
          f"~{estimate:.1f}s of {total:.1f}s by recorded durations", flush=True)
    return check, TestScope(selection.mode, selected, collected, selection.reason, selection.detail)


def _select_config(selection: Selection | None, index: int, shards: int, output: Path) -> dict:
    if selection is not None:
        return selection.plugin_config(index, shards, output)
    return {"affected": None, "known": [], "test_files": [], "durations": {},
            "shard_index": index, "shard_count": shards, "output": str(output)}


def _combine_checks(checks: list[Check]) -> Check:
    if len(checks) == 1:
        return checks[0]
    statuses = {check.status for check in checks}
    status = "error" if "error" in statuses else "fail" if "fail" in statuses else "pass"
    counted = [check for check in checks if check.total is not None]
    passed = sum(check.passed or 0 for check in counted)
    total = sum(check.total or 0 for check in counted)
    width = 2000 // len(checks) - 20
    summary = "\n".join(f"[shard {index + 1}/{len(checks)}] {check.summary[-width:]}"
                        for index, check in enumerate(checks))
    return Check(status, summary[:2000], passed if total else None, total or None)


def _merge_traces(directories: list[Path]) -> dict:
    callers: dict[str, dict[tuple, int]] = {}
    callees: dict[str, dict[str, int]] = {}
    tests: dict[str, list[str]] = {}
    for directory in directories:
        part = _read_json(directory / "trace.json") or {}
        for symbol, chains in part.get("callers", {}).items():
            merged = callers.setdefault(symbol, {})
            for item in chains:
                chain = tuple(item["chain"])
                if chain in merged or len(merged) < MAX_TRACE_CHAINS:
                    merged[chain] = merged.get(chain, 0) + item["count"]
        for symbol, items in part.get("callees", {}).items():
            merged_callees = callees.setdefault(symbol, {})
            for item in items:
                merged_callees.setdefault(item["callee"], item["line"])
        for symbol, nodeids in part.get("tests", {}).items():
            merged_tests = tests.setdefault(symbol, [])
            for nodeid in nodeids:
                if nodeid not in merged_tests and len(merged_tests) < MAX_TRACE_TESTS:
                    merged_tests.append(nodeid)
    return {
        "version": 1,
        "callers": {symbol: [{"chain": list(chain), "count": count} for chain, count in chains.items()]
                    for symbol, chains in callers.items()},
        "callees": {symbol: [{"callee": callee, "line": line} for callee, line in items.items()]
                    for symbol, items in callees.items()},
        "tests": tests,
    }


def _write_test_map(directories: list[Path], head_sha: str, output: Path) -> None:
    parts = []
    for directory in directories:
        path = directory / "record.json"
        if not path.is_file():
            raise RuntimeError("pytest exited before writing the test impact map")
        parts.append(TestMap.from_plugin(path.read_text(encoding="utf-8"), head_sha))
    test_map = TestMap.merge(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(test_map.to_json() + "\n", encoding="utf-8")
    print(f"Test impact map written to {output}: {len(test_map.tests)} tests, "
          f"{len(test_map.symbols)} functions", flush=True)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def merge_evidence(parts: list[Evidence]) -> Evidence:
    if not parts or len({part.head_sha for part in parts}) != 1:
        raise ValueError("evidence parts must belong to one PR head SHA")

    def select(name: str) -> Check:
        checks = [getattr(part, name) for part in parts if getattr(part, name).status != "not_run"]
        if len(checks) != 1:
            raise ValueError(f"evidence requires exactly one {name} result")
        return checks[0]

    pytest_part = next((part for part in parts if part.pytest.status != "not_run"), None)
    return Evidence(parts[0].head_sha, select("django_check"), select("migration_check"), select("pytest"),
                    pytest_part.test_scope if pytest_part else None)


def safe_evidence_environment(home: Path, policy: EnvironmentPolicy = EnvironmentPolicy()) -> dict[str, str]:
    allowed = {name.upper() for name in (*SAFE_ENVIRONMENT_NAMES, *policy.pass_env)}
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    if policy.django_settings_module:
        environment["DJANGO_SETTINGS_MODULE"] = policy.django_settings_module
    environment.update({name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    # Django's makemigrations opens a real DB connection to check migration-history
    # consistency even with --check --dry-run --noinput. libpq/psycopg honor this to
    # fail fast instead of blocking on the OS TCP timeout when that DB is unreachable
    # from CI (observed ~25s per attempt without it).
    environment["PGCONNECT_TIMEOUT"] = "5"
    return environment


def _run(command: list[str], repository: Path, timeout: int, environment: dict[str, str],
         success: tuple[int, ...] = (0,)) -> Check:
    label = " ".join(command[1:])
    print(f"Starting check: {label}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=repository, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                                env=environment, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Check stopped after {time.monotonic() - started:.1f}s: {type(exc).__name__}", flush=True)
        return Check("error", type(exc).__name__)
    print(f"Check finished in {time.monotonic() - started:.1f}s with exit code {result.returncode}", flush=True)
    return Check("pass" if result.returncode in success else "fail", _summary(result.stdout, result.stderr))


def _python_changed(repository: Path, base_sha: str, head_sha: str,
                    environment: dict[str, str]) -> bool:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, head_sha, "--", "*.py"],
            cwd=repository, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=True, env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not determine whether Python files changed") from exc
    return bool(result.stdout.strip())


def _run_pytest(command: list[str], repository: Path, timeout: int, environment: dict[str, str],
                allow_empty: bool = False) -> Check:
    # Under selection or sharding, a run whose tests were all deselected is a pass, not a failure.
    check = _run(command, repository, timeout, environment, (0, NO_TESTS_COLLECTED) if allow_empty else (0,))
    counts: dict[str, int] = {}
    for count, name in re.findall(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)", check.summary):
        counts[name] = counts.get(name, 0) + int(count)
    passed = counts.get("passed", 0)
    total = sum(counts.values())
    return Check(check.status, check.summary, passed if total else None, total or None)


def _run_migration(command: list[str], repository: Path, timeout: int, environment: dict[str, str]) -> Check:
    check = _run(command, repository, timeout, environment)
    if check.status == "fail" and "Migrations for" not in check.summary:
        return Check("error", check.summary)
    return check


def _summary(stdout: str, stderr: str) -> str:
    lines = [line.strip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()]
    return "\n".join(lines[-20:])[:2000]
