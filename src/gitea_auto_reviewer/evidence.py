"""Deterministic evidence collected by an unprivileged CI job."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .git_context import validate_sha

STATUSES = {"pass", "fail", "error", "not_run"}
SAFE_ENVIRONMENT_NAMES = {
    "CI", "COMSPEC", "DJANGO_SETTINGS_MODULE", "LANG", "LC_ALL", "NOX_MES_CI", "NOX_MES_CI_LIVE_PATH",
    "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PYTHONIOENCODING", "PYTHONPATH", "PYTHONUTF8", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
}


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

    @classmethod
    def from_json(cls, raw: str) -> Evidence:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("evidence is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"version", "head_sha", "checks"}:
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
        )

    def to_json(self) -> str:
        return json.dumps(
            {"version": 1, "head_sha": self.head_sha, "checks": {
                "django_check": asdict(self.django_check),
                "migration_check": asdict(self.migration_check),
                "pytest": asdict(self.pytest),
            }},
            ensure_ascii=False,
            indent=2,
        )


def collect_evidence(repository: Path, head_sha: str, python: str = "python", timeout: int = 900,
                     only: str | None = None, base_sha: str | None = None) -> Evidence:
    head_sha = validate_sha(head_sha)
    if only not in {None, "django_check", "migration_check", "pytest"}:
        raise ValueError("unknown evidence check")
    with tempfile.TemporaryDirectory(prefix="gitea-evidence-") as home:
        environment = safe_evidence_environment(Path(home))
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
                    [python, "manage.py", "makemigrations", "--check", "--dry-run"],
                    repository, timeout, environment,
                )
        return Evidence(head_sha,
            _run([python, "manage.py", "check"], repository, timeout, environment)
            if only in {None, "django_check"} else not_run,
            migration,
            _run_pytest([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], repository, timeout, environment)
            if only in {None, "pytest"} else not_run,
        )


def merge_evidence(parts: list[Evidence]) -> Evidence:
    if not parts or len({part.head_sha for part in parts}) != 1:
        raise ValueError("evidence parts must belong to one PR head SHA")

    def select(name: str) -> Check:
        checks = [getattr(part, name) for part in parts if getattr(part, name).status != "not_run"]
        if len(checks) != 1:
            raise ValueError(f"evidence requires exactly one {name} result")
        return checks[0]

    return Evidence(parts[0].head_sha, select("django_check"), select("migration_check"), select("pytest"))


def safe_evidence_environment(home: Path) -> dict[str, str]:
    allowed = {name.upper() for name in SAFE_ENVIRONMENT_NAMES}
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update({name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    return environment


def _run(command: list[str], repository: Path, timeout: int, environment: dict[str, str]) -> Check:
    label = " ".join(command[1:])
    print(f"Starting check: {label}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=repository, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                                env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Check stopped after {time.monotonic() - started:.1f}s: {type(exc).__name__}", flush=True)
        return Check("error", type(exc).__name__)
    print(f"Check finished in {time.monotonic() - started:.1f}s with exit code {result.returncode}", flush=True)
    return Check("pass" if result.returncode == 0 else "fail", _summary(result.stdout, result.stderr))


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


def _run_pytest(command: list[str], repository: Path, timeout: int, environment: dict[str, str]) -> Check:
    check = _run(command, repository, timeout, environment)
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
