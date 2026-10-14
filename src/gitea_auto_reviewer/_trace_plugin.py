"""pytest plugin that observes which repository code each test really executes.

Loaded with ``-p`` from a temporary directory during the evidence pytest run, so it executes in
the same process as PR code: its output has the same trust level as the pytest result itself.
It must not import gitea_auto_reviewer because the project's CI interpreter does not have it.

Each section of the JSON config named by ``GITEA_AUTO_REVIEWER_TRACE`` enables one job:
- ``trace``: record real call chains into and out of PR-changed functions
- ``record``: record a test impact map (functions each test executed, durations, outcomes)
- ``select``: keep only the tests a change can affect and this process's duration-balanced shard
"""

import heapq
import json
import os
import sys
import threading
from pathlib import Path

import pytest

CONFIG_ENV = "GITEA_AUTO_REVIEWER_TRACE"
MAX_CHAIN = 8
MAX_CHAINS = 20
MAX_TESTS = 20
EXCLUDED_PARTS = {".venv", "venv", "site-packages", "node_modules", ".git"}
DEFAULT_DURATION = 1.0
# Only function code gets fresh locals; module and class bodies run at import and are not mapped.
CO_NEWLOCALS = 0x0002


def shard_unit(nodeid):
    """Tests sharing a class or module stay on one shard so class/module setup runs once."""
    parts = nodeid.split("[", 1)[0].split("::")
    return "::".join(parts[:2]) if len(parts) > 2 else parts[0]


def assign_shards(durations, count):
    """Longest-processing-time-first packing of shard units; deterministic for equal input."""
    units = {}
    for nodeid, seconds in durations.items():
        unit = shard_unit(nodeid)
        units[unit] = units.get(unit, 0.0) + seconds
    loads = [(0.0, index) for index in range(count)]
    placement = {}
    for unit, seconds in sorted(units.items(), key=lambda item: (-item[1], item[0])):
        load, index = heapq.heappop(loads)
        placement[unit] = index
        heapq.heappush(loads, (load + seconds, index))
    return {nodeid: placement[shard_unit(nodeid)] for nodeid in durations}


class Tracer:
    def __init__(self, config):
        self.repository = Path(config["repository"]).resolve()
        trace = config.get("trace") or {}
        self.trace_output = Path(trace["output"]) if trace else None
        self.changed = {path: [tuple(item) for item in ranges] for path, ranges in trace.get("changed", {}).items()}
        record = config.get("record") or {}
        self.record_output = Path(record["output"]) if record else None
        self.codes = {}
        self.callers = {}
        self.callees = {}
        self.tests = {}
        self.active = []
        self.current_test = None
        # Impact-map recording: symbol table plus per-test symbol sets.
        self.symbols = {}
        self.outside = set()
        self.executed = {}
        self.durations = {}
        self.outcomes = {}
        # Code run while a fixture (including unittest setUpClass) is set up, keyed by fixture,
        # and the fixture closure of every test that depends on it.
        self.fixture_stack = []
        self.fixture_code = {}
        self.closures = {}

    def describe(self, code):
        try:
            return self.codes[code]
        except KeyError:
            pass
        described, path = None, None
        # Frozen/generated code ("<frozen abc>", "<string>") would otherwise resolve under the cwd.
        if not code.co_filename.startswith("<"):
            try:
                path = Path(code.co_filename).resolve().relative_to(self.repository)
            except (ValueError, OSError):
                path = None
        # Template engines compile to code objects named after non-Python files; only .py counts.
        if path is not None and path.suffix == ".py" and not EXCLUDED_PARTS.intersection(path.parts):
            relative = path.as_posix()
            lines = [line for _start, _end, line in code.co_lines() if line]
            last = max(lines, default=code.co_firstlineno)
            changed = any(start <= last and code.co_firstlineno <= end
                          for start, end in self.changed.get(relative, ()))
            name = getattr(code, "co_qualname", code.co_name)
            described = (f"{relative}:{name}", code.co_firstlineno, changed)
        self.codes[code] = described
        return described

    def symbol_index(self, symbol):
        return self.symbols.setdefault(symbol, len(self.symbols))

    def cover(self, code, symbol):
        if not code.co_flags & CO_NEWLOCALS:
            return
        index = self.symbol_index(symbol)
        if self.fixture_stack:
            self.fixture_code.setdefault(self.fixture_stack[-1], set()).add(index)
        if self.current_test is None:
            self.outside.add(index)
        else:
            self.executed.setdefault(self.current_test, set()).add(index)

    def profile(self, frame, event, arg):
        if event == "call":
            described = self.describe(frame.f_code)
            if described is None:
                return
            symbol, line, changed = described
            if self.record_output is not None:
                self.cover(frame.f_code, symbol)
            if self.trace_output is None:
                return
            if changed:
                chain, caller = [], frame.f_back
                while caller is not None and len(chain) < MAX_CHAIN:
                    outer = self.describe(caller.f_code)
                    if outer is not None:
                        chain.append(outer[0])
                    caller = caller.f_back
                chains = self.callers.setdefault(symbol, {})
                if tuple(chain) in chains or len(chains) < MAX_CHAINS:
                    chains[tuple(chain)] = chains.get(tuple(chain), 0) + 1
                if self.current_test:
                    tests = self.tests.setdefault(symbol, [])
                    if self.current_test not in tests and len(tests) < MAX_TESTS:
                        tests.append(self.current_test)
                self.active.append((id(frame), symbol))
            elif self.active and not symbol.endswith("<module>"):
                self.callees.setdefault(self.active[-1][1], {}).setdefault(symbol, line)
        elif event == "return" and self.active and self.active[-1][0] == id(frame):
            self.active.pop()

    def thread_profile(self, frame, event, arg):
        """Other threads only feed the impact map; call chains stay on the main thread's stack."""
        if event == "call" and self.record_output is not None:
            described = self.describe(frame.f_code)
            if described is not None:
                self.cover(frame.f_code, described[0])

    def fixtures(self, item):
        """Remember a test's fixture closure: a session- or class-scoped fixture is set up during
        the first test that needs it, yet every test that depends on it depends on that code."""
        if self.record_output is None:
            return
        info = getattr(item, "_fixtureinfo", None)
        self.closures[item.nodeid] = [id(definition)
                                      for definitions in getattr(info, "name2fixturedefs", {}).values()
                                      for definition in definitions]

    def write(self):
        if self.trace_output is not None:
            self.trace_output.write_text(json.dumps({
                "version": 1,
                "callers": {symbol: [{"chain": list(chain), "count": count} for chain, count in chains.items()]
                            for symbol, chains in self.callers.items()},
                "callees": {symbol: [{"callee": callee, "line": line} for callee, line in callees.items()]
                            for symbol, callees in self.callees.items()},
                "tests": self.tests,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.record_output is not None:
            tests = {}
            for nodeid in self.outcomes:
                covers = set(self.executed.get(nodeid, ()))
                for fixture in self.closures.get(nodeid, ()):
                    covers.update(self.fixture_code.get(fixture, ()))
                tests[nodeid] = {"duration": round(self.durations.get(nodeid, 0.0), 4),
                                 "outcome": self.outcomes[nodeid], "covers": sorted(covers)}
            self.record_output.write_text(json.dumps({
                "version": 1,
                "symbols": sorted(self.symbols, key=self.symbols.get),
                "outside": sorted(self.outside),
                "tests": tests,
            }, ensure_ascii=False), encoding="utf-8")


class Selector:
    def __init__(self, config):
        select = config["select"]
        affected = select["affected"]
        self.affected = None if affected is None else set(affected)
        self.known = set(select["known"])
        self.test_files = set(select["test_files"])
        self.durations = {nodeid: float(seconds) for nodeid, seconds in select["durations"].items()}
        self.shard_index = select["shard_index"]
        self.shard_count = select["shard_count"]
        self.output = Path(select["output"])

    def keep(self, nodeid):
        # Tests absent from the map are new or renamed, so they always run.
        return (self.affected is None or nodeid in self.affected or nodeid not in self.known
                or nodeid.split("::", 1)[0] in self.test_files)

    def apply(self, config, items):
        collected = len(items)
        selected = [item for item in items if self.keep(item.nodeid)]
        known = sorted(self.durations.values())
        # Tests without a recorded duration are estimated at the median of those with one.
        fallback = known[len(known) // 2] if known else DEFAULT_DURATION
        estimates = {item.nodeid: self.durations.get(item.nodeid, fallback) for item in selected}
        placement = assign_shards(estimates, self.shard_count)
        mine = [item for item in selected if placement[item.nodeid] == self.shard_index]
        kept = {id(item) for item in mine}
        dropped = [item for item in items if id(item) not in kept]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = mine
        self.output.write_text(json.dumps({
            "collected": collected,
            "selected": len(selected),
            "shard_tests": len(mine),
            "shard_seconds": round(sum(estimates[item.nodeid] for item in mine), 3),
        }), encoding="utf-8")


_tracer = None
_selector = None


def _start():
    global _tracer, _selector
    path = os.environ.get(CONFIG_ENV)
    if not path:
        return
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("select"):
        _selector = Selector(config)
    if config.get("trace") or config.get("record"):
        _tracer = Tracer(config)
        # Started at import (``-p`` loads this before conftest files) so code that runs while
        # conftest imports the project, such as Django's AppConfig.ready, is observed too.
        sys.setprofile(_tracer.profile)
        threading.setprofile(_tracer.thread_profile)


_start()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    if _selector is not None:
        _selector.apply(config, items)


def pytest_runtest_logstart(nodeid, location):
    if _tracer is not None:
        _tracer.current_test = nodeid


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if _tracer is not None:
        _tracer.fixtures(item)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if _tracer is not None:
        _tracer.fixture_stack.append(id(fixturedef))
    try:
        yield
    finally:
        if _tracer is not None:
            _tracer.fixture_stack.pop()


def pytest_runtest_logreport(report):
    if _tracer is None:
        return
    nodeid = report.nodeid
    _tracer.durations[nodeid] = _tracer.durations.get(nodeid, 0.0) + (report.duration or 0.0)
    previous = _tracer.outcomes.get(nodeid, "passed")
    outcome = "failed" if report.failed else ("skipped" if report.skipped and previous != "failed" else previous)
    _tracer.outcomes[nodeid] = outcome


def pytest_runtest_logfinish(nodeid, location):
    if _tracer is not None:
        _tracer.current_test = None


def pytest_unconfigure(config):
    if _tracer is not None:
        sys.setprofile(None)
        threading.setprofile(None)
        _tracer.write()
