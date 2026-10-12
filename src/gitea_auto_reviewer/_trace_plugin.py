"""pytest plugin that records real call chains into and out of PR-changed functions.

Loaded with ``-p`` from a temporary directory during the evidence pytest run, so it executes in
the same process as PR code: its output has the same trust level as the pytest result itself.
It must not import gitea_auto_reviewer because the project's CI interpreter does not have it.
"""

import json
import os
import sys
from pathlib import Path

CONFIG_ENV = "GITEA_AUTO_REVIEWER_TRACE"
MAX_CHAIN = 8
MAX_CHAINS = 20
MAX_TESTS = 20
EXCLUDED_PARTS = {".venv", "venv", "site-packages", "node_modules", ".git"}


class Tracer:
    def __init__(self, config):
        self.repository = Path(config["repository"]).resolve()
        self.changed = {path: [tuple(item) for item in ranges] for path, ranges in config["changed"].items()}
        self.output = Path(config["output"])
        self.codes = {}
        self.callers = {}
        self.callees = {}
        self.tests = {}
        self.active = []
        self.current_test = None

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
        if path is not None and not EXCLUDED_PARTS.intersection(path.parts):
            relative = path.as_posix()
            lines = [line for _start, _end, line in code.co_lines() if line]
            last = max(lines, default=code.co_firstlineno)
            changed = any(start <= last and code.co_firstlineno <= end
                          for start, end in self.changed.get(relative, ()))
            name = getattr(code, "co_qualname", code.co_name)
            described = (f"{relative}:{name}", code.co_firstlineno, changed)
        self.codes[code] = described
        return described

    def profile(self, frame, event, arg):
        if event == "call":
            described = self.describe(frame.f_code)
            if described is None:
                return
            symbol, line, changed = described
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

    def write(self):
        payload = {
            "version": 1,
            "callers": {symbol: [{"chain": list(chain), "count": count} for chain, count in chains.items()]
                        for symbol, chains in self.callers.items()},
            "callees": {symbol: [{"callee": callee, "line": line} for callee, line in callees.items()]
                        for symbol, callees in self.callees.items()},
            "tests": self.tests,
        }
        self.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


_tracer = None


def pytest_configure(config):
    global _tracer
    path = os.environ.get(CONFIG_ENV)
    if not path:
        return
    _tracer = Tracer(json.loads(Path(path).read_text(encoding="utf-8")))
    sys.setprofile(_tracer.profile)


def pytest_runtest_logstart(nodeid, location):
    if _tracer is not None:
        _tracer.current_test = nodeid


def pytest_unconfigure(config):
    if _tracer is not None:
        sys.setprofile(None)
        _tracer.write()
