"""Change-aware test selection from a test impact map recorded on the trusted base branch.

A test impact map records, for one commit, which repository functions each test executed (with
fixture and class/module setup code attributed to every test that depends on it), its duration,
and its outcome. Selection compares that commit with the PR head at function granularity using
only git objects, so no PR code runs before the decision:

- a test runs when it executed a changed or removed function, failed when the map was recorded,
  is absent from the map (new or renamed), or lives in a changed test module;
- the whole suite runs when a change cannot be bounded by function coverage: module- or
  class-level code, a deleted or renamed Python file, a non-Python file, a configured full-run
  path, or a changed function that ran outside any test (imports, collection, session hooks).
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import ReviewConfig
from .git_context import validate_sha
from .review import FULL_RUN_REASONS

MAP_VERSION = 1
MAX_TESTS = 100_000
MAX_SYMBOLS = 500_000
MAX_NODEID = 1000
MAX_UNCOVERED = 50
MAX_DETAIL = 300
OUTCOMES = {"passed", "failed", "skipped"}
_SYMBOL = re.compile(r"[^\n:]+\.py:[^\s:]+")


@dataclass(frozen=True)
class TestRecord:
    nodeid: str
    duration: float
    outcome: str
    covers: tuple[int, ...]


@dataclass(frozen=True)
class TestMap:
    commit: str
    symbols: tuple[str, ...]
    outside: tuple[int, ...]
    tests: tuple[TestRecord, ...]

    @classmethod
    def from_json(cls, raw: str) -> TestMap:
        value = _load(raw, "test impact map")
        if not isinstance(value, dict) or set(value) != {"version", "commit", "symbols", "outside", "tests"} \
                or value["version"] != MAP_VERSION:
            raise ValueError("invalid test impact map")
        return cls._validated(validate_sha(value["commit"]), value)

    @classmethod
    def from_plugin(cls, raw: str, commit: str) -> TestMap:
        """Validate the raw recording written by the pytest plugin and bind it to a commit."""
        value = _load(raw, "test impact recording")
        if not isinstance(value, dict) or set(value) != {"version", "symbols", "outside", "tests"} \
                or value["version"] != MAP_VERSION:
            raise ValueError("invalid test impact recording")
        return cls._validated(validate_sha(commit), value)

    @classmethod
    def _validated(cls, commit: str, value: dict) -> TestMap:
        symbols, tests = value["symbols"], value["tests"]
        if not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS or not all(
            isinstance(item, str) and len(item) <= 500 and _SYMBOL.fullmatch(item) and _safe_path(item.split(":", 1)[0])
            for item in symbols
        ) or len(set(symbols)) != len(symbols):
            raise ValueError("test impact map has invalid symbols")
        if not isinstance(tests, dict) or len(tests) > MAX_TESTS:
            raise ValueError("test impact map has invalid tests")
        records = []
        for nodeid, record in tests.items():
            if not _nodeid(nodeid) or not isinstance(record, dict) or set(record) != {"duration", "outcome", "covers"}:
                raise ValueError("test impact map has an invalid test entry")
            duration, outcome = record["duration"], record["outcome"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
                    or not math.isfinite(duration) or not 0 <= duration <= 86_400 or outcome not in OUTCOMES:
                raise ValueError(f"test impact map has an invalid test entry: {nodeid}")
            records.append(TestRecord(nodeid, float(duration), outcome,
                                      _indexes(record["covers"], len(symbols))))
        return cls(commit, tuple(symbols), _indexes(value["outside"], len(symbols)),
                   tuple(sorted(records, key=lambda item: item.nodeid)))

    def to_json(self) -> str:
        return json.dumps({
            "version": MAP_VERSION,
            "commit": self.commit,
            "symbols": list(self.symbols),
            "outside": list(self.outside),
            "tests": {item.nodeid: {"duration": item.duration, "outcome": item.outcome, "covers": list(item.covers)}
                      for item in self.tests},
        }, ensure_ascii=False)

    @classmethod
    def merge(cls, parts: list[TestMap]) -> TestMap:
        """Combine maps recorded by parallel shards of one commit."""
        if not parts or len({part.commit for part in parts}) != 1:
            raise ValueError("test impact map parts must belong to one commit")
        symbols: dict[str, int] = {}
        outside: set[int] = set()
        tests: dict[str, TestRecord] = {}
        for part in parts:
            renumber = [symbols.setdefault(symbol, len(symbols)) for symbol in part.symbols]
            outside.update(renumber[index] for index in part.outside)
            for record in part.tests:
                if record.nodeid in tests:
                    raise ValueError(f"test recorded by more than one shard: {record.nodeid}")
                tests[record.nodeid] = TestRecord(record.nodeid, record.duration, record.outcome,
                                                  tuple(sorted(renumber[index] for index in record.covers)))
        return cls(parts[0].commit, tuple(symbols), tuple(sorted(outside)),
                   tuple(sorted(tests.values(), key=lambda item: item.nodeid)))


@dataclass(frozen=True)
class ChangeSet:
    """Function-level difference between the map commit and the PR head."""

    symbols: frozenset[str]
    test_files: tuple[str, ...]
    full_run: tuple[str, str] | None = None


@dataclass(frozen=True)
class Selection:
    head_sha: str
    mode: str
    reason: str | None
    detail: str | None
    map_commit: str | None
    affected: tuple[str, ...] | None
    test_files: tuple[str, ...]
    durations: dict[str, float]
    uncovered: tuple[str, ...] = ()

    @property
    def estimated_seconds(self) -> tuple[float, float]:
        """Recorded seconds of the affected map tests and of the whole map."""
        total = sum(self.durations.values())
        if self.affected is None:
            return total, total
        return sum(self.durations.get(nodeid, 0.0) for nodeid in self.affected), total

    def plugin_config(self, shard_index: int, shard_count: int, output: Path) -> dict:
        return {
            "affected": None if self.affected is None else list(self.affected),
            "known": list(self.durations),
            "test_files": list(self.test_files),
            "durations": self.durations,
            "shard_index": shard_index,
            "shard_count": shard_count,
            "output": str(output),
        }

    def to_json(self) -> str:
        return json.dumps({
            "version": 1,
            "head_sha": self.head_sha,
            "mode": self.mode,
            "reason": self.reason,
            "detail": self.detail,
            "map_commit": self.map_commit,
            "affected": None if self.affected is None else list(self.affected),
            "test_files": list(self.test_files),
            "durations": self.durations,
            "uncovered": list(self.uncovered),
        }, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> Selection:
        value = _load(raw, "test selection")
        if not isinstance(value, dict) or set(value) != {
            "version", "head_sha", "mode", "reason", "detail", "map_commit", "affected", "test_files",
            "durations", "uncovered",
        } or value["version"] != 1:
            raise ValueError("invalid test selection")
        mode, reason, detail = value["mode"], value["reason"], value["detail"]
        if mode == "impact":
            if reason is not None or detail is not None or not isinstance(value["affected"], list):
                raise ValueError("impact selection must list affected tests and no full-run reason")
        elif mode != "full" or reason not in FULL_RUN_REASONS or value["affected"] is not None:
            raise ValueError("full selection requires a known reason")
        if detail is not None and (not isinstance(detail, str) or len(detail) > MAX_DETAIL or "\n" in detail):
            raise ValueError("invalid test selection detail")
        map_commit = None if value["map_commit"] is None else validate_sha(value["map_commit"])
        affected = None if value["affected"] is None else _nodeids(value["affected"])
        test_files = value["test_files"]
        if not isinstance(test_files, list) or not all(isinstance(item, str) and _safe_path(item) for item in test_files):
            raise ValueError("invalid test selection files")
        durations = value["durations"]
        if not isinstance(durations, dict) or len(durations) > MAX_TESTS or not all(
            _nodeid(nodeid) and not isinstance(seconds, bool) and isinstance(seconds, (int, float))
            and math.isfinite(seconds) and 0 <= seconds <= 86_400 for nodeid, seconds in durations.items()
        ):
            raise ValueError("invalid test selection durations")
        uncovered = value["uncovered"]
        if not isinstance(uncovered, list) or len(uncovered) > MAX_UNCOVERED or not all(
            isinstance(item, str) and _SYMBOL.fullmatch(item) for item in uncovered
        ):
            raise ValueError("invalid test selection symbols")
        return cls(validate_sha(value["head_sha"]), mode, reason, detail, map_commit, affected,
                   tuple(test_files), {nodeid: float(seconds) for nodeid, seconds in durations.items()},
                   tuple(uncovered))


def full_selection(head_sha: str, reason: str, detail: str | None = None,
                   test_map: TestMap | None = None) -> Selection:
    if reason not in FULL_RUN_REASONS:
        raise ValueError(f"unknown full-run reason: {reason}")
    durations = {item.nodeid: item.duration for item in test_map.tests} if test_map else {}
    return Selection(validate_sha(head_sha), "full", reason, _detail(detail),
                     test_map.commit if test_map else None, None, (), durations)


def select_tests(repository: Path, head_sha: str, test_map: TestMap, config: ReviewConfig) -> Selection:
    head_sha = validate_sha(head_sha)
    if not _is_ancestor(repository, test_map.commit, head_sha):
        return full_selection(head_sha, "map_not_ancestor", test_map.commit[:12], test_map)
    changes = analyze_changes(repository, test_map, head_sha, config)
    if changes.full_run is not None:
        return full_selection(head_sha, *changes.full_run, test_map)
    index = {symbol: position for position, symbol in enumerate(test_map.symbols)}
    changed = {index[symbol] for symbol in changes.symbols if symbol in index}
    outside = sorted(changed.intersection(test_map.outside))
    if outside:
        return full_selection(head_sha, "outside_test", test_map.symbols[outside[0]], test_map)
    affected = tuple(item.nodeid for item in test_map.tests
                     if item.outcome == "failed" or changed.intersection(item.covers))
    uncovered = tuple(sorted(symbol for symbol in changes.symbols if symbol not in index))[:MAX_UNCOVERED]
    return Selection(head_sha, "impact", None, None, test_map.commit, affected, changes.test_files,
                     {item.nodeid: item.duration for item in test_map.tests}, uncovered)


def analyze_changes(repository: Path, test_map: TestMap, head_sha: str, config: ReviewConfig) -> ChangeSet:
    test_modules = {item.nodeid.split("::", 1)[0] for item in test_map.tests}
    ignored = (*config.documentation_paths, *config.test_ignore_paths)
    symbols: set[str] = set()
    test_files: list[str] = []
    for status, path in _changed_paths(repository, test_map.commit, head_sha):
        if _matches(path, config.test_full_run_paths):
            return ChangeSet(frozenset(), (), ("full_run_path", path))
        if not path.endswith(".py"):
            if not _matches(path, ignored):
                return ChangeSet(frozenset(), (), ("non_python", path))
            continue
        if status in {"D", "R"}:
            return ChangeSet(frozenset(), (), ("removed_file", path))
        old = _shape(_blob(repository, test_map.commit, path)) if status != "A" else ("", {})
        new = _shape(_blob(repository, head_sha, path))
        if old is None or new is None:
            return ChangeSet(frozenset(), (), ("module_level", path))
        if path in test_modules:
            test_files.append(path)
        elif status != "A" and old[0] != new[0]:
            return ChangeSet(frozenset(), (), ("module_level", path))
        names = set(old[1]) | set(new[1])
        symbols.update(f"{path}:{name}" for name in names if old[1].get(name) != new[1].get(name))
    return ChangeSet(frozenset(symbols), tuple(sorted(test_files)))


def _shape(source: str | None) -> tuple[str, dict[str, str]] | None:
    """Split a module into its module/class skeleton and a dump of every function by qualname.

    Docstrings and line numbers are ignored. A class keeps the names of its methods in its
    skeleton, so adding or removing a method (which can change dispatch) is a class-level change.
    """
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    _drop_docstrings(tree)
    functions: dict[str, str] = {}

    def collect(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[prefix + child.name] = ast.dump(child)
                collect(child, f"{prefix}{child.name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                collect(child, f"{prefix}{child.name}.")
            else:
                collect(child, prefix)

    def strip(body: list[ast.stmt], in_class: bool) -> list[ast.stmt]:
        kept: list[ast.stmt] = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if in_class:
                    kept.append(ast.Expr(ast.Constant(f"def {node.name}")))
            elif isinstance(node, ast.ClassDef):
                node.body = strip(node.body, True)
                kept.append(node)
            else:
                kept.append(node)
        return kept

    collect(tree, "")
    skeleton = copy.deepcopy(tree)
    skeleton.body = strip(skeleton.body, False)
    return ast.dump(skeleton), functions


def _drop_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body[0] = ast.Pass()


def _changed_paths(repository: Path, old: str, new: str) -> list[tuple[str, str]]:
    result = _git(repository, "diff", "--name-status", "-z", "-M", "--no-ext-diff", "--no-color",
                  validate_sha(old), validate_sha(new), "--")
    fields = result.split("\0")
    changes = []
    position = 0
    while position < len(fields) and fields[position]:
        status = fields[position][0]
        if status in {"R", "C"}:
            # A rename or copy reports both paths; the source disappears for a rename.
            source, target = fields[position + 1], fields[position + 2]
            changes.append(("R" if status == "R" else "A", source if status == "R" else target))
            if status == "R":
                changes.append(("A", target))
            position += 3
        else:
            changes.append((status if status in {"A", "D"} else "M", fields[position + 1]))
            position += 2
    return changes


def _blob(repository: Path, commit: str, path: str) -> str | None:
    try:
        return _git(repository, "show", f"{validate_sha(commit)}:{path}")
    except RuntimeError:
        return None


def _is_ancestor(repository: Path, ancestor: str, head: str) -> bool:
    try:
        result = subprocess.run(["git", "merge-base", "--is-ancestor", validate_sha(ancestor), validate_sha(head)],
                                cwd=repository, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not compare the test impact map commit with the PR head") from exc
    return result.returncode == 0


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git {arguments[0]} failed") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git {arguments[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _load(raw: str, name: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON") from exc


def _indexes(value: object, size: int) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item < size for item in value
    ):
        raise ValueError("test impact map has invalid symbol references")
    return tuple(sorted(set(value)))


def _nodeid(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_NODEID or "\n" in value or "\r" in value:
        return False
    return _safe_path(value.split("::", 1)[0])


def _nodeids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_TESTS or not all(_nodeid(item) for item in value):
        raise ValueError("invalid test node IDs")
    return tuple(value)


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value \
        and ":" not in value


def _detail(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value[:MAX_DETAIL - 1] + "…" if len(value) > MAX_DETAIL else value
