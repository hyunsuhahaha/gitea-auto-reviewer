"""Deterministic change-impact paths that a static call graph misses in Django projects.

Three sources are combined and tagged:
- django: model metadata, signal receivers, and URL routes read from the live Django app registry
- orm: string-based ORM field references (``filter(stock__lt=…)``, ``F("stock")``) found by AST
- runtime: call chains recorded while the evidence pytest run executed changed functions
Each path is then checked against GitNexus impact results to flag edges the static graph lacks.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from .evidence import safe_evidence_environment
from .git_context import validate_sha
from .review import ImpactPath, impact_path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__", "migrations",
                  ".gitnexus"}
READ_METHODS = {"filter", "exclude", "get", "values", "values_list", "order_by", "only", "defer",
                "annotate", "aggregate", "select_related", "prefetch_related", "distinct"}
WRITE_METHODS = {"create", "update", "bulk_create", "bulk_update", "delete", "get_or_create",
                 "update_or_create"}
STRING_FIELD_METHODS = {"values", "values_list", "order_by", "only", "defer", "select_related",
                        "prefetch_related", "distinct", "bulk_update"}
MAX_PATHS = 40


@dataclass(frozen=True)
class ChangedFunction:
    symbol: str
    file: str
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class OrmReference:
    model: str
    field: str
    access: str
    file: str
    line: int
    function: str | None


def changed_ranges(repository: Path, base_sha: str, head_sha: str) -> dict[str, list[tuple[int, int]]]:
    """Head-side changed line ranges for every changed Python file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--no-color", "--no-ext-diff", "-U0",
             f"{validate_sha(base_sha)}...{validate_sha(head_sha)}", "--", "*.py"],
            cwd=repository, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not read base-to-head Python diff") from exc
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and (match := HUNK.match(line)):
            start, count = int(match.group(1)), int(match.group(2) or "1")
            ranges.setdefault(current, []).append((max(start, 1), max(start, 1) + max(count, 1) - 1))
    return ranges


def _python_files(repository: Path) -> list[str]:
    files = []
    for path in sorted(repository.rglob("*.py")):
        relative = path.relative_to(repository)
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        files.append(relative.as_posix())
    return files


def _is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return ("tests" in PurePosixPath(path).parts or name.startswith("test_") or name == "conftest.py"
            or name.endswith(("_test.py", "_spec.py")))


class _Functions(ast.NodeVisitor):
    """Collect function spans with Python-compatible qualnames."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.functions: list[tuple[str, int, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        self.functions.append((".".join([*self.stack, node.name]), start, node.end_lineno or node.lineno))
        self.stack.extend([node.name, "<locals>"])
        self.generic_visit(node)
        del self.stack[-2:]

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function


def _parse(repository: Path, path: str) -> ast.Module | None:
    try:
        return ast.parse((repository / path).read_text(encoding="utf-8-sig"), filename=path)
    except (OSError, SyntaxError, ValueError):
        return None


def changed_functions(repository: Path, ranges: dict[str, list[tuple[int, int]]]) -> list[ChangedFunction]:
    changed = []
    for path, spans in sorted(ranges.items()):
        tree = _parse(repository, path)
        if tree is None:
            continue
        visitor = _Functions()
        visitor.visit(tree)
        for qualname, start, end in visitor.functions:
            if any(first <= end and start <= last for first, last in spans):
                changed.append(ChangedFunction(f"{path}:{qualname}", path, qualname, start, end))
    return changed


def orm_references(repository: Path, models: list[dict]) -> list[OrmReference]:
    """Find ORM calls whose receiver chain starts at a known model class name."""
    by_name: dict[str, dict] = {}
    duplicates = set()
    for model in models:
        if model["name"] in by_name:
            duplicates.add(model["name"])
        by_name[model["name"]] = model
    for name in duplicates:
        by_name.pop(name, None)
    references: list[OrmReference] = []
    for path in _python_files(repository):
        if _is_test_file(path):
            continue
        tree = _parse(repository, path)
        if tree is None:
            continue
        visitor = _Functions()
        visitor.visit(tree)
        spans = sorted(visitor.functions, key=lambda item: item[2] - item[1])
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method not in READ_METHODS | WRITE_METHODS:
                continue
            model = by_name.get(_receiver_model(node.func.value) or "")
            if model is None:
                continue
            names = {field["name"]: field["name"] for field in model["fields"]}
            names.update({field["attname"]: field["name"] for field in model["fields"]})
            access = "write" if method in WRITE_METHODS else "read"
            function = next((f"{path}:{qualname}" for qualname, start, end in spans
                             if start <= node.lineno <= end), None)
            for field in sorted(_call_fields(node, method)):
                if field in names:
                    references.append(OrmReference(model["label"], names[field], access, path,
                                                   node.lineno, function))
    return references


def _receiver_model(node: ast.AST) -> str | None:
    saw_manager = False
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            saw_manager = saw_manager or node.attr in {"objects", "_base_manager", "_default_manager"}
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id if saw_manager else None
        else:
            return None


def _call_fields(node: ast.Call, method: str) -> set[str]:
    fields = {keyword.arg.split("__", 1)[0] for keyword in node.keywords if keyword.arg}
    if method in STRING_FIELD_METHODS:
        fields.update(argument.value.lstrip("-").split("__", 1)[0] for argument in node.args
                      if isinstance(argument, ast.Constant) and isinstance(argument.value, str))
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in {"F", "Q"}:
            fields.update(argument.value.split("__", 1)[0] for argument in child.args
                          if isinstance(argument, ast.Constant) and isinstance(argument.value, str))
            fields.update(keyword.arg.split("__", 1)[0] for keyword in child.keywords if keyword.arg)
    return fields


def extract_django(repository: Path, python: str, timeout: int = 300) -> dict:
    """Run the Django extractor with the project's interpreter inside the evidence boundary."""
    with tempfile.TemporaryDirectory(prefix="gitea-django-graph-") as directory:
        root = Path(directory)
        script, output = root / "extract.py", root / "django-graph.json"
        script.write_text(Path(__file__).with_name("_django_extract.py").read_text(encoding="utf-8"),
                          encoding="utf-8")
        (root / "home").mkdir()
        environment = safe_evidence_environment(root / "home")
        try:
            result = subprocess.run([python, str(script), str(output)], cwd=repository, env=environment,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Django structure extraction failed: {type(exc).__name__}") from exc
        if result.returncode or not output.exists():
            detail = " ".join((result.stderr or result.stdout).split())[-1000:]
            raise RuntimeError(f"Django structure extraction failed: {detail}")
        return json.loads(output.read_text(encoding="utf-8"))


def build_paths(changed: list[ChangedFunction], django: dict, references: list[OrmReference],
                runtime: dict | None, ranges: dict[str, list[tuple[int, int]]]) -> list[ImpactPath]:
    paths: dict[tuple, ImpactPath] = {}

    def add(path: ImpactPath) -> None:
        if path.source != path.target:
            paths.setdefault((path.kind, path.source, path.target, path.detail), path)

    by_function: dict[str, list[OrmReference]] = {}
    for reference in references:
        if reference.function:
            by_function.setdefault(reference.function, []).append(reference)
    receivers: dict[str, list[dict]] = {}
    for item in django.get("signals", []):
        receivers.setdefault(item["sender"] or "*", []).append(item)
    for function in changed:
        own = by_function.get(function.symbol, [])
        written_models = {item.model for item in own if item.access == "write"}
        for model in sorted(written_models):
            for item in receivers.get(model, []) + receivers.get("*", []):
                if not item["signal"].startswith(("pre_", "post_", "m2m_")):
                    continue
                receiver = item["receiver"]
                add(ImpactPath("signal", function.symbol, f"{receiver['file']}:{receiver['name']}",
                               f"{model.rsplit('.', 1)[-1]} {item['signal']}",
                               f"{receiver['file']}:{receiver['line']}", "django", "downstream"))
        written_fields = {(item.model, item.field) for item in own if item.access == "write"}
        for reference in references:
            if (reference.model, reference.field) in written_fields and reference.function \
                    and reference.function != function.symbol:
                add(ImpactPath("orm_field", function.symbol, reference.function,
                               f"{reference.model.rsplit('.', 1)[-1]}.{reference.field} {_access(reference.access)}",
                               f"{reference.file}:{reference.line}", "orm", "downstream"))
        for item in django.get("signals", []):
            receiver = item["receiver"]
            if f"{receiver['file']}:{receiver['name']}" != function.symbol or not item["sender"]:
                continue
            for reference in references:
                if reference.model == item["sender"] and reference.access == "write" and reference.function:
                    add(ImpactPath("signal", reference.function, function.symbol,
                                   f"{item['sender'].rsplit('.', 1)[-1]} {item['signal']}",
                                   f"{reference.file}:{reference.line}", "django", "upstream"))
        for route in django.get("urls", []):
            view = route["view"]
            if f"{view['file']}:{view['name']}" == function.symbol:
                add(ImpactPath("url", route["route"], function.symbol, "URL 진입점",
                               f"{view['file']}:{view['line']}", "django", "upstream"))
        for chain in (runtime or {}).get("callers", {}).get(function.symbol, []):
            if chain["chain"]:
                entry = chain["chain"][-1]
                add(ImpactPath("runtime_caller", chain["chain"][0], function.symbol,
                               f"실행 체인 진입점 {entry}", chain["chain"][0], "runtime", "upstream"))
        for callee in (runtime or {}).get("callees", {}).get(function.symbol, []):
            add(ImpactPath("runtime_callee", function.symbol, callee["callee"], "pytest 실행 중 호출",
                           f"{callee['callee'].rsplit(':', 1)[0]}:{callee['line']}", "runtime", "downstream"))
    for model in django.get("models", []):
        for field in model["fields"]:
            line = field.get("line")
            if not line or not any(first <= line <= last for first, last in ranges.get(model["file"], [])):
                continue
            source = f"{model['file']}:{model['name']}.{field['name']}"
            for reference in references:
                if reference.model == model["label"] and reference.field == field["name"] and reference.function:
                    add(ImpactPath("orm_field", source, reference.function,
                                   f"{model['name']}.{field['name']} 정의 변경 · {_access(reference.access)}",
                                   f"{reference.file}:{reference.line}", "orm", "downstream"))
    # A runtime edge that repeats a Django/ORM edge confirms it instead of being listed twice.
    structural = {(item.source, item.target): key for key, item in paths.items() if item.origin != "runtime"}
    for key, item in list(paths.items()):
        confirmed = structural.get((item.source, item.target))
        if item.origin == "runtime" and confirmed is not None:
            del paths[key]
            original = paths[confirmed]
            if "pytest 실행으로 확인" not in original.detail:
                paths[confirmed] = replace(original, detail=f"{original.detail} · pytest 실행으로 확인")
    return sorted(paths.values(), key=lambda item: (item.kind, item.source, item.target))[:MAX_PATHS]


def _access(access: str) -> str:
    return "쓰기" if access == "write" else "읽기"


def mark_static_misses(paths: list[ImpactPath], static: dict[tuple[str, str], set[tuple[str, str]]]) -> list[ImpactPath]:
    """Flag paths whose far end is absent from GitNexus impact for the changed symbol."""
    marked = []
    for path in paths:
        changed, other = (path.source, path.target) if path.direction == "downstream" else (path.target, path.source)
        reached = static.get((changed, path.direction))
        if reached is None or path.kind == "url":
            marked.append(path)
            continue
        file, _separator, qualname = other.rpartition(":")
        missed = (file, qualname.rsplit(".", 1)[-1]) not in reached
        marked.append(ImpactPath(**{**asdict(path), "static_missed": missed}))
    return marked


def analyze_impact(repository: Path, base_sha: str, head_sha: str, python: str,
                   runtime: dict | None = None, gitnexus=None) -> dict:
    """Build the impact document; ``gitnexus`` is a callable(symbol_file, name, direction) or None."""
    repository = repository.resolve()
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True,
                            text=True, check=False).stdout.strip().lower()
    if actual != validate_sha(head_sha):
        raise ValueError("impact checkout does not match the supplied PR head SHA")
    ranges = changed_ranges(repository, base_sha, head_sha)
    changed = changed_functions(repository, ranges)
    sources = {"runtime": "collected" if runtime else "not_collected"}
    try:
        django = extract_django(repository, python)
        sources["django"] = "collected" if not django.get("url_error") else f"partial: {django['url_error']}"
    except RuntimeError as exc:
        django, sources["django"] = {"models": [], "signals": [], "urls": []}, f"error: {exc}"[:500]
    references = orm_references(repository, django.get("models", []))
    paths = build_paths(changed, django, references, runtime, ranges)
    static_status = "not_requested"
    if gitnexus is not None and paths:
        static: dict[tuple[str, str], set[tuple[str, str]]] = {}
        try:
            for function in changed[:20]:
                for direction in ("upstream", "downstream"):
                    static[(function.symbol, direction)] = gitnexus(
                        function.file, function.name.rsplit(".", 1)[-1], direction
                    )
            paths = mark_static_misses(paths, static)
            static_status = "compared"
        except RuntimeError as exc:
            static_status = f"unavailable: {exc}"[:500]
    return {
        "version": 1,
        "base_sha": validate_sha(base_sha),
        "head_sha": validate_sha(head_sha),
        "changed_symbols": [function.symbol for function in changed],
        "sources": {**sources, "gitnexus": static_status},
        "paths": [asdict(path) for path in paths],
    }


def load_paths(raw: str, head_sha: str) -> tuple[ImpactPath, ...]:
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("head_sha") != validate_sha(head_sha):
        raise ValueError("impact document belongs to a different PR head SHA")
    return tuple(impact_path(item) for item in value.get("paths", []))
