import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gitea_auto_reviewer import _trace_plugin
from gitea_auto_reviewer.cli import main
from gitea_auto_reviewer.config import ReviewConfig
from gitea_auto_reviewer.evidence import Evidence, collect_evidence
from gitea_auto_reviewer.review import TestScope as Scope
from gitea_auto_reviewer.selection import Selection, full_selection, select_tests
from gitea_auto_reviewer.selection import TestMap as ImpactMap

ENABLED = ReviewConfig(test_selection=True)

PRICING = '''"""Pricing rules."""

RATE = 2


def price(amount):
    return amount * RATE


def tax(amount):
    return amount + 1


def load_regions():
    return ["kr"]


REGIONS = load_regions()


class Cart:
    def __init__(self, amount):
        self.amount = amount

    def total(self):
        return price(self.amount)
'''

TESTS = '''import unittest

import pytest

from app.pricing import Cart, price, tax
from app.support import prepare


@pytest.fixture(scope="session")
def catalog():
    return prepare()


def test_price(catalog):
    assert price(2) == 4


def test_tax(catalog):
    assert tax(2) == 3


def test_cart():
    assert Cart(3).total() == 6


class SetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = prepare()

    def test_first(self):
        assert self.ready

    def test_second(self):
        assert self.ready
'''

SUPPORT = '''def prepare():
    return True
'''


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
         "-c", "core.autocrlf=false", *arguments],
        cwd=repository, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repository: Path, files: dict[str, str | None], message: str) -> str:
    for path, content in files.items():
        target = repository / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path):
    repository = tmp_path / "project"
    repository.mkdir()
    _git(repository, "init", "-q")
    base = _commit(repository, {
        ".gitignore": "__pycache__/\n",
        "manage.py": "",
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
        "app/__init__.py": "",
        "app/pricing.py": PRICING,
        "app/support.py": SUPPORT,
        "tests/__init__.py": "",
        "tests/test_pricing.py": TESTS,
        "README.md": "# project\n",
    }, "base")
    return repository, base


def _record(repository: Path, commit: str, tmp_path: Path, **kwargs) -> ImpactMap:
    _git(repository, "checkout", "-q", commit)
    output = tmp_path / f"map-{commit[:7]}.json"
    evidence = collect_evidence(repository, commit, sys.executable, 120, "pytest", record_map=output, **kwargs)
    assert evidence.pytest.status == "pass", evidence.pytest.summary
    return ImpactMap.from_json(output.read_text(encoding="utf-8"))


def _change(repository: Path, base: str, files: dict[str, str | None]) -> str:
    _git(repository, "checkout", "-q", base)
    _git(repository, "checkout", "-q", "-B", "change")
    return _commit(repository, files, "change")


def _test(name: str) -> str:
    return f"tests/test_pricing.py::{name}"


def test_recorded_map_attributes_functions_fixtures_and_class_setup_to_tests(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)

    covers = {item.nodeid: {test_map.symbols[index] for index in item.covers} for item in test_map.tests}
    assert test_map.commit == base
    assert set(covers) == {_test(name) for name in (
        "test_price", "test_tax", "test_cart", "SetupTest::test_first", "SetupTest::test_second")}
    assert "app/pricing.py:price" in covers[_test("test_cart")]
    assert "app/pricing.py:tax" not in covers[_test("test_price")]
    # The session fixture ran once, during test_price, but both of its users depend on it.
    assert "tests/test_pricing.py:catalog" in covers[_test("test_tax")]
    # setUpClass ran during test_first only, yet test_second shares that class-level setup.
    assert "app/support.py:prepare" in covers[_test("SetupTest::test_second")]
    # load_regions ran while the test module was imported, outside any test.
    assert "app/pricing.py:load_regions" in {test_map.symbols[index] for index in test_map.outside}
    assert all(item.outcome == "passed" and item.duration >= 0 for item in test_map.tests)


def test_function_change_runs_only_tests_that_executed_it(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, {"app/pricing.py": PRICING.replace("amount * RATE", "RATE * amount")})

    selection = select_tests(repository, head, test_map, ENABLED)

    assert selection.mode == "impact"
    assert set(selection.affected) == {_test("test_price"), _test("test_cart")}
    evidence = collect_evidence(repository, head, sys.executable, 120, "pytest", selection=selection)
    assert (evidence.pytest.status, evidence.pytest.passed, evidence.pytest.total) == ("pass", 2, 2)
    assert evidence.test_scope == Scope("impact", 2, 5)


def test_shared_setup_change_selects_every_dependent_test(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, {"app/support.py": "def prepare():\n    return 1\n"})

    selection = select_tests(repository, head, test_map, ENABLED)

    assert set(selection.affected) == {_test("test_price"), _test("test_tax"),
                                       _test("SetupTest::test_first"), _test("SetupTest::test_second")}


def test_docstring_only_change_runs_no_mapped_tests(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, {
        "app/pricing.py": PRICING.replace('"""Pricing rules."""', '"""Pricing rules for orders."""'),
        "README.md": "# project\n\nmore docs\n",
    })

    selection = select_tests(repository, head, test_map, ENABLED)
    evidence = collect_evidence(repository, head, sys.executable, 120, "pytest", selection=selection)

    assert selection.mode == "impact" and selection.affected == ()
    assert evidence.pytest.status == "pass"
    assert evidence.test_scope == Scope("impact", 0, 5)


def test_new_tests_and_changed_test_modules_always_run(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, {
        "tests/test_new.py": "def test_added():\n    assert True\n",
        "tests/test_pricing.py": TESTS.replace("assert self.ready\n\n    def test_second",
                                               "assert self.ready is True\n\n    def test_second"),
    })

    selection = select_tests(repository, head, test_map, ENABLED)
    evidence = collect_evidence(repository, head, sys.executable, 120, "pytest", selection=selection)

    assert selection.test_files == ("tests/test_pricing.py",)
    assert (evidence.pytest.passed, evidence.pytest.total) == (6, 6)
    assert evidence.test_scope == Scope("impact", 6, 6)


def test_previously_failing_tests_are_always_selected(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    failing = replace(test_map, tests=tuple(
        replace(item, outcome="failed") if item.nodeid == _test("test_tax") else item for item in test_map.tests
    ))
    head = _change(repository, base, {"README.md": "changed\n"})

    assert select_tests(repository, head, failing, ENABLED).affected == (_test("test_tax"),)


@pytest.mark.parametrize("files, reason, detail", [
    ({"app/pricing.py": PRICING.replace("RATE = 2", "RATE = 3")}, "module_level", "app/pricing.py"),
    ({"app/pricing.py": PRICING + "\n    def discount(self):\n        return 0\n"},
     "module_level", "app/pricing.py"),
    ({"app/pricing.py": PRICING.replace('return ["kr"]', 'return ["kr", "jp"]')},
     "outside_test", "app/pricing.py:load_regions"),
    ({"app/support.py": None}, "removed_file", "app/support.py"),
    ({"app/prices.json": "{}"}, "non_python", "app/prices.json"),
    ({"requirements.txt": "django\n"}, "non_python", "requirements.txt"),
])
def test_changes_that_function_coverage_cannot_bound_run_the_full_suite(project, tmp_path, files, reason,
                                                                         detail) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, files)

    selection = select_tests(repository, head, test_map, ENABLED)

    assert (selection.mode, selection.reason, selection.detail, selection.affected) == ("full", reason, detail, None)


def test_configured_paths_force_or_skip_a_full_run(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    head = _change(repository, base, {"deploy/app.yaml": "x: 1\n", "app/support.py": SUPPORT + "\n"})

    ignored = select_tests(repository, head, test_map, replace(ENABLED, test_ignore_paths=("deploy/*",)))
    forced = select_tests(repository, head, test_map, replace(ENABLED, test_full_run_paths=("support.py",)))

    assert ignored.mode == "impact"
    assert (forced.reason, forced.detail) == ("full_run_path", "app/support.py")


def test_map_from_an_unrelated_commit_is_not_used(project, tmp_path) -> None:
    repository, base = project
    head = _change(repository, base, {"app/support.py": "def prepare():\n    return 2\n"})
    test_map = _record(repository, head, tmp_path)
    other = _change(repository, base, {"app/support.py": "def prepare():\n    return 3\n"})

    selection = select_tests(repository, other, test_map, ENABLED)

    assert (selection.mode, selection.reason) == ("full", "map_not_ancestor")


def test_shards_split_by_recorded_duration_and_keep_classes_together() -> None:
    durations = {"a.py::slow": 10.0, "a.py::fast": 1.0, "b.py::K::one": 4.0, "b.py::K::two": 4.0,
                 "c.py::x[1]": 2.0, "c.py::x[2]": 2.0}

    placement = _trace_plugin.assign_shards(durations, 2)

    assert placement["b.py::K::one"] == placement["b.py::K::two"]
    loads = [sum(seconds for nodeid, seconds in durations.items() if placement[nodeid] == index)
             for index in range(2)]
    assert sorted(loads) == [11.0, 12.0]
    assert placement == _trace_plugin.assign_shards(dict(reversed(durations.items())), 2)


def test_parallel_shards_run_every_selected_test_once_and_merge_the_map(project, tmp_path) -> None:
    repository, base = project
    sharded = _record(repository, base, tmp_path, shards=2)
    single = _record(repository, base, tmp_path)

    assert [item.nodeid for item in sharded.tests] == [item.nodeid for item in single.tests]
    symbols = lambda test_map: {item.nodeid: {test_map.symbols[index] for index in item.covers}
                                for item in test_map.tests}
    assert symbols(sharded) == symbols(single)
    selection = full_selection(base, "no_map", test_map=single)
    evidence = collect_evidence(repository, base, sys.executable, 120, "pytest", selection=selection, shards=3)
    assert (evidence.pytest.status, evidence.pytest.passed, evidence.pytest.total) == ("pass", 5, 5)
    assert evidence.pytest.summary.count("[shard ") == 3
    assert evidence.test_scope == Scope("full", 5, 5, "no_map", None)


def test_selection_and_map_documents_are_validated(project, tmp_path) -> None:
    repository, base = project
    test_map = _record(repository, base, tmp_path)
    selection = select_tests(repository, base, test_map, ENABLED)

    assert ImpactMap.from_json(test_map.to_json()) == test_map
    assert Selection.from_json(selection.to_json()) == selection
    document = json.loads(test_map.to_json())
    for broken in ({"symbols": ["../escape.py:f"]}, {"symbols": ["C:/abs.py:f"]},
                   {"tests": {"tests/test_x.py::t": {"duration": -1, "outcome": "passed", "covers": []}}},
                   {"tests": {"tests/test_x.py::t": {"duration": 1, "outcome": "passed", "covers": [10**6]}}}):
        with pytest.raises(ValueError):
            ImpactMap.from_json(json.dumps({**document, **broken}))
    with pytest.raises(ValueError):
        Selection.from_json(selection.to_json().replace('"impact"', '"full"'))
    with pytest.raises(ValueError, match="different PR head"):
        collect_evidence(repository, "f" * 40, sys.executable, 1, "pytest", selection=selection)


def test_test_select_command_falls_back_without_a_valid_map(project, tmp_path, capsys) -> None:
    repository, base = project
    config = tmp_path / "config.json"
    output = tmp_path / "selection.json"
    arguments = ["test-select", "--head-sha", base, "--repo-dir", str(repository),
                 "--test-map", str(tmp_path / "missing.json"), "--output", str(output), "--config-file", str(config)]

    config.write_text(ReviewConfig().to_json(), encoding="utf-8")
    output.write_text("stale", encoding="utf-8")
    assert main(arguments) == 0
    assert not output.exists()

    config.write_text(ENABLED.to_json(), encoding="utf-8")
    assert main(arguments) == 0
    assert Selection.from_json(output.read_text(encoding="utf-8")).reason == "no_map"
    (tmp_path / "missing.json").write_text('{"version": 1}', encoding="utf-8")
    assert main(arguments) == 0
    assert Selection.from_json(output.read_text(encoding="utf-8")).reason == "invalid_map"


def test_test_select_ignores_a_map_committed_by_the_pull_request(project, tmp_path) -> None:
    repository, base = project
    real = _record(repository, base, tmp_path)
    planted = replace(real, tests=tuple(replace(item, covers=()) for item in real.tests))
    head = _change(repository, base, {
        ".ai-review-test-map/test-map.json": planted.to_json(),
        "app/pricing.py": PRICING.replace("amount + 1", "amount + 2"),
    })
    config = tmp_path / "config.json"
    config.write_text(ENABLED.to_json(), encoding="utf-8")
    output = tmp_path / "selection.json"

    assert main(["test-select", "--head-sha", head, "--repo-dir", str(repository), "--test-map",
                 str(repository / ".ai-review-test-map" / "test-map.json"), "--output", str(output),
                 "--config-file", str(config)]) == 0
    assert Selection.from_json(output.read_text(encoding="utf-8")).reason == "invalid_map"


def test_evidence_scope_round_trips_and_merges() -> None:
    from gitea_auto_reviewer.evidence import Check, merge_evidence

    scope = Scope("full", 7, 7, "module_level", "app/models.py")
    passed, not_run = Check("pass", "7 passed", 7, 7), Check("not_run", "not selected")
    parts = [Evidence("a" * 40, Check("pass", "ok"), not_run, not_run),
             Evidence("a" * 40, not_run, Check("pass", "ok"), not_run),
             Evidence("a" * 40, not_run, not_run, passed, scope)]

    merged = merge_evidence(parts)

    assert merged.test_scope == scope
    assert Evidence.from_json(merged.to_json()) == merged
    assert "test_scope" not in json.loads(parts[0].to_json())
