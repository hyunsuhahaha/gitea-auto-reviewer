"""Base-versus-head differential execution: availability, checkout, and deterministic comparison."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .git_context import validate_sha

DEPENDENCY_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile(\.lock)?|poetry\.lock|uv\.lock)$"
)
PREDICTION = re.compile(r"^(result|exception|[A-Za-z_]\w*(\.[A-Za-z_]\w*){0,2})$")
MISSING_ENTRYPOINT_ERRORS = ("ImportError", "ModuleNotFoundError")
MAX_DIFFERENCES = 10


def differential_blocker(repository: Path, base_sha: str, head_sha: str) -> str | None:
    """Return why base/head cannot share one venv and test DB, or None when they can."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{validate_sha(base_sha)}...{validate_sha(head_sha)}"],
            cwd=repository, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not list base-to-head changed files") from exc
    paths = [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
    migrations = [path for path in paths if "/migrations/" in f"/{path}" and path.endswith(".py")]
    if migrations:
        return f"마이그레이션 변경 PR이라 같은 테스트 DB에서 base와 비교할 수 없음: {migrations[0]}"
    dependencies = [path for path in paths if DEPENDENCY_FILES.search(path)]
    if dependencies:
        return f"의존성 파일 변경 PR이라 같은 venv에서 base를 실행할 수 없음: {dependencies[0]}"
    return None


@contextmanager
def base_worktree(repository: Path, base_sha: str, head_sha: str) -> Iterator[Path]:
    """Check out the merge base of the PR as a detached, temporary worktree.

    The merge base, not the moving base-branch tip, isolates exactly the PR's own change,
    matching the ``base...head`` diff the review stage reads.
    """
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", validate_sha(base_sha), validate_sha(head_sha)], cwd=repository,
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not resolve the PR merge base") from exc
    with tempfile.TemporaryDirectory(prefix="gitea-base-") as directory:
        path = Path(directory) / "base"
        _git(repository, ["worktree", "add", "--detach", "--force", str(path), validate_sha(merge_base)])
        try:
            yield path
        finally:
            _git(repository, ["worktree", "remove", "--force", str(path)], required=False)
            _git(repository, ["worktree", "prune"], required=False)


def _git(repository: Path, arguments: list[str], required: bool = True) -> None:
    try:
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        if required:
            raise RuntimeError(f"git {arguments[0]} failed for the base worktree") from exc


def validate_predictions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("predicted_changes must be a list of at most 10 items")
    items = []
    for item in value:
        if not isinstance(item, str) or not PREDICTION.fullmatch(item.strip()):
            raise ValueError("predicted_changes items must be Model, Model.field, app.Model.field, result, or exception")
        items.append(item.strip())
    return tuple(dict.fromkeys(items))


@dataclass(frozen=True)
class Fact:
    """One comparable observation: a model/field location plus a display-ready before value."""

    model: str | None
    field: str | None
    label: str
    value: str
    before: str | None = None
    note: str = ""


def outcome_facts(outcome: dict) -> dict[tuple, Fact]:
    """Flatten one captured run into keyed facts with run-specific identifiers normalized away."""
    facts: dict[tuple, Fact] = {}
    exception = outcome.get("exception")
    facts[("exception",)] = Fact(None, "exception", "예외", exception or "없음")
    if exception is None:
        facts[("result",)] = Fact(None, "result", "반환값", _canonical(outcome.get("result")))
    for change in outcome.get("changes", []):
        model, pk = change["model"], change["pk"]
        short = model.rsplit(".", 1)[-1]
        volatile = set(change.get("volatile", ())) | {pk}
        created = change.get("created", [])
        facts[(model, "created_count")] = Fact(model, None, f"{short} 생성", f"{len(created)}건")
        for index, row in enumerate(created, start=1):
            for name, value in row.items():
                if name in volatile:
                    continue
                facts[(model, "created", index, name)] = Fact(
                    model, name, f"{short}[신규#{index}].{name}", _canonical(value)
                )
        for row in change.get("deleted", []):
            facts[(model, "deleted", _canonical(row.get(pk)))] = Fact(
                model, None, f"{short}(pk={row.get(pk)}) 삭제", "삭제됨"
            )
        for item in change.get("updated", []):
            for name, value in item["after"].items():
                if name in volatile or item["before"].get(name) == value:
                    continue
                facts[(model, "updated", _canonical(item["pk"]), name)] = Fact(
                    model, name, f"{short}(pk={item['pk']}).{name}", _canonical(value),
                    _canonical(item["before"].get(name)),
                )
    # Foreign keys to rows created in the same run are compared by creation order, not raw id.
    created_ids = {
        (change["model"], row.get(change["pk"])): f"<new:{change['model'].rsplit('.', 1)[-1]}#{index}>"
        for change in outcome.get("changes", [])
        for index, row in enumerate(change.get("created", []), start=1)
    }
    for change in outcome.get("changes", []):
        for attname, target in change.get("foreign_keys", {}).items():
            for key, fact in list(facts.items()):
                if key[0] == change["model"] and fact.field == attname:
                    raw = json.loads(fact.value) if fact.value not in {"", "없음"} else None
                    placeholder = created_ids.get((target, raw))
                    if placeholder:
                        facts[key] = Fact(fact.model, fact.field, fact.label, placeholder, fact.before)
    # Summarize the first created row on its count fact; the note is display-only, never compared.
    for key, fact in list(facts.items()):
        if len(key) == 2 and key[1] == "created_count" and fact.value != "0건":
            fields = [f"{item.field}={item.value}" for row_key, item in facts.items()
                      if row_key[:3] == (key[0], "created", 1)]
            facts[key] = Fact(fact.model, fact.field, fact.label, fact.value, note=", ".join(fields)[:150])
    return facts


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class Comparison:
    status: str
    summary: str
    predicted: tuple[str, ...]
    unpredicted: tuple[str, ...]
    ignored_fields: int


def compare_runs(base_runs: list[dict], head_runs: list[dict], predictions: tuple[str, ...]) -> Comparison:
    """Decide confirmed/refuted/inconclusive from repeated base and head captures."""
    for exception in (run.get("exception") or "" for run in base_runs):
        if exception.split(":", 1)[0] in MISSING_ENTRYPOINT_ERRORS:
            return Comparison("inconclusive", f"base에 시나리오 진입점이 없음: {exception}"[:1000], (), (), 0)
    base_facts = [outcome_facts(run) for run in base_runs]
    head_facts = [outcome_facts(run) for run in head_runs]
    noisy = _noisy_keys(base_facts) | _noisy_keys(head_facts)
    base, head = base_facts[0], head_facts[0]
    base_rows = {key[:3] for key in base if len(key) == 4}
    head_rows = {key[:3] for key in head if len(key) == 4}
    differences = []
    for key in sorted(set(base) | set(head), key=lambda item: json.dumps(item, default=str)):
        if key in noisy:
            continue
        # A row created on only one side is already described by its "생성 N건" line.
        if len(key) == 4 and key[1] == "created" and (key[:3] in base_rows) != (key[:3] in head_rows):
            continue
        before, after = _side(base, key, head), _side(head, key, base)
        if before is not None and after is not None and before.value == after.value:
            continue
        differences.append((before, after))
    predicted, unpredicted = [], []
    for before, after in differences:
        fact = after or before
        line = _difference_line(fact, before, after)
        (predicted if _matches(fact, predictions) else unpredicted).append(line)
    predicted, unpredicted = predicted[:MAX_DIFFERENCES], unpredicted[:MAX_DIFFERENCES]
    if not differences:
        return Comparison("refuted", "base와 head의 DB 변경·반환값·예외가 모두 같음", (), (), len(noisy))
    if predicted:
        summary = "base 대비 예측한 차이 관찰: " + "; ".join(predicted[:3])
        return Comparison("confirmed", summary[:1000], tuple(predicted), tuple(unpredicted), len(noisy))
    summary = "예측한 위치의 차이는 없고 다른 차이만 관찰: " + "; ".join(unpredicted[:3])
    return Comparison("refuted", summary[:1000], (), tuple(unpredicted), len(noisy))


def _side(facts: dict[tuple, Fact], key: tuple, other: dict[tuple, Fact]) -> Fact | None:
    """A table the other run wrote but this run did not counts as zero created rows."""
    if key in facts or len(key) != 2 or key[1] != "created_count":
        return facts.get(key)
    fact = other[key]
    return Fact(fact.model, fact.field, fact.label, "0건")


def _noisy_keys(runs: list[dict[tuple, Fact]]) -> set[tuple]:
    noisy: set[tuple] = set()
    for other in runs[1:]:
        for key in set(runs[0]) | set(other):
            first, second = runs[0].get(key), other.get(key)
            if first is None or second is None or first.value != second.value:
                noisy.add(key)
    return noisy


def _matches(fact: Fact, predictions: tuple[str, ...]) -> bool:
    for prediction in predictions:
        if prediction in {"result", "exception"}:
            if fact.model is None and fact.field == prediction:
                return True
            continue
        if fact.model is None:
            continue
        parts = prediction.split(".")
        if len(parts) == 3:
            app, model, field = parts
        elif len(parts) == 2 and parts[0][:1].isupper():
            app, model, field = None, parts[0], parts[1]
        elif len(parts) == 2:
            app, model, field = parts[0], parts[1], None
        else:
            app, model, field = None, parts[0], None
        fact_app, fact_model = fact.model.rsplit(".", 1)
        if model.lower() != fact_model.lower() or (app is not None and app.lower() != fact_app.lower()):
            continue
        if field is None or fact.field is None or _field_name(field) == _field_name(fact.field):
            return True
    return False


def _field_name(name: str) -> str:
    return name[:-3] if name.endswith("_id") else name


def _difference_line(fact: Fact, before: Fact | None, after: Fact | None) -> str:
    def side(item: Fact | None) -> str:
        if item is None:
            return "변경 없음" if (before or after).before is not None else "없음"
        if item.before is not None:
            return f"{item.before} → {item.value}"
        return f"{item.value} ({item.note})" if item.note else item.value

    return f"{fact.label}  base {side(before)} · head {side(after)}"[:300]
