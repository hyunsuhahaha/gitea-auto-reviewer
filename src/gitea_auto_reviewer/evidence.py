"""Deterministic evidence collected by an unprivileged CI job."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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


def collect_evidence(repository: Path, head_sha: str, python: str = "python", timeout: int = 900) -> Evidence:
    head_sha = validate_sha(head_sha)
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
        return Evidence(
            head_sha,
            _run([python, "manage.py", "check"], repository, timeout, environment),
            _run_migration([python, "manage.py", "makemigrations", "--check", "--dry-run"], repository, timeout, environment),
            _run_pytest([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], repository, timeout, environment),
        )


def safe_evidence_environment(home: Path) -> dict[str, str]:
    allowed = {name.upper() for name in SAFE_ENVIRONMENT_NAMES}
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update({name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    return environment


def _run(command: list[str], repository: Path, timeout: int, environment: dict[str, str]) -> Check:
    try:
        result = subprocess.run(command, cwd=repository, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                                env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("error", type(exc).__name__)
    return Check("pass" if result.returncode == 0 else "fail", _summary(result.stdout, result.stderr))


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
