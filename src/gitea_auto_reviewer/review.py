"""Compact change-impact validation and rendering."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

FIELDS = {
    "changed_files",
    "database_change",
    "django_check",
    "migration",
    "api_contract",
    "external_integration",
    "tests",
    "risk",
    "key_changes",
    "new_assumptions",
    "cautions",
    "human_checks",
}

REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": sorted(FIELDS),
    "properties": {
        "changed_files": {"type": "integer", "minimum": 1},
        "database_change": {"enum": ["yes", "no", "possible"]},
        "django_check": {"enum": ["pass", "fail", "error", "not_run"]},
        "migration": {"enum": ["no_missing", "missing", "error", "not_run"]},
        "api_contract": {"enum": ["changed", "unchanged", "possible"]},
        "external_integration": {"enum": ["affected", "unchanged", "possible"]},
        "tests": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "passed", "total"],
            "properties": {
                "status": {"enum": ["pass", "fail", "error", "not_run"]},
                "passed": {"type": ["integer", "null"], "minimum": 0},
                "total": {"type": ["integer", "null"], "minimum": 0},
            },
        },
        "risk": {"enum": ["low", "medium", "high"]},
        "key_changes": {"$ref": "#/$defs/requiredItems"},
        "new_assumptions": {"$ref": "#/$defs/requiredItems"},
        "cautions": {"$ref": "#/$defs/optionalItems"},
        "human_checks": {"$ref": "#/$defs/requiredItems"},
    },
    "$defs": {
        "requiredItems": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "optionalItems": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
}


@dataclass(frozen=True)
class TestResult:
    status: str
    passed: int | None
    total: int | None

    @classmethod
    def from_value(cls, value: object) -> TestResult:
        if not isinstance(value, dict) or set(value) != {"status", "passed", "total"}:
            raise ValueError("tests must contain status, passed, and total")
        status, passed, total = value["status"], value["passed"], value["total"]
        if status not in {"pass", "fail", "error", "not_run"}:
            raise ValueError("invalid test status")
        if status in {"error", "not_run"}:
            if passed is not None or total is not None:
                raise ValueError("unavailable tests must use null counts")
        elif not (_count(passed) and _count(total) and passed <= total):
            raise ValueError("run tests require valid passed and total counts")
        return cls(status, passed, total)


@dataclass(frozen=True)
class Review:
    changed_files: int
    database_change: str
    django_check: str
    migration: str
    api_contract: str
    external_integration: str
    tests: TestResult
    risk: str
    key_changes: tuple[str, ...]
    new_assumptions: tuple[str, ...]
    cautions: tuple[str, ...]
    human_checks: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: str) -> Review:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Codex output is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("review output does not match the change-impact schema")
        changed_files = value["changed_files"]
        if not _count(changed_files) or changed_files < 1:
            raise ValueError("changed_files must be a positive integer")
        _enum(value, "database_change", {"yes", "no", "possible"})
        _enum(value, "django_check", {"pass", "fail", "error", "not_run"})
        _enum(value, "migration", {"no_missing", "missing", "error", "not_run"})
        _enum(value, "api_contract", {"changed", "unchanged", "possible"})
        _enum(value, "external_integration", {"affected", "unchanged", "possible"})
        _enum(value, "risk", {"low", "medium", "high"})
        return cls(
            changed_files=changed_files,
            database_change=value["database_change"],
            django_check=value["django_check"],
            migration=value["migration"],
            api_contract=value["api_contract"],
            external_integration=value["external_integration"],
            tests=TestResult.from_value(value["tests"]),
            risk=value["risk"],
            key_changes=_items(value["key_changes"], "key_changes", required=True),
            new_assumptions=_items(value["new_assumptions"], "new_assumptions", required=True),
            cautions=_items(value["cautions"], "cautions", required=False),
            human_checks=_items(value["human_checks"], "human_checks", required=True),
        )

    def to_json(self) -> str:
        value = asdict(self)
        for name in ("key_changes", "new_assumptions", "cautions", "human_checks"):
            value[name] = list(value[name])
        return json.dumps(value, ensure_ascii=False, indent=2)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _enum(value: dict[str, object], name: str, allowed: set[str]) -> None:
    if value[name] not in allowed:
        raise ValueError(f"invalid {name}")


def _items(value: object, name: str, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 5 or (required and not value):
        raise ValueError(f"{name} must contain {'1-5' if required else '0-5'} items")
    items = tuple(item.strip() for item in value if isinstance(item, str))
    if len(items) != len(value) or any(not item or len(item) > 500 for item in items):
        raise ValueError(f"{name} contains an invalid item")
    return items


def render_markdown(review: Review, pr_number: int, head_sha: str, pr_title: str) -> str:
    title = " ".join(pr_title.split())[:120]
    heading = f"PR #{pr_number} {title}"
    width = max(28, _display_width(heading) + 2)
    top = f"┌{'─' * width}┐"
    middle = f"├{'─' * width}┤"
    heading_line = f"│ {heading}{' ' * (width - _display_width(heading) - 1)}│"
    lines = [
        top,
        heading_line,
        middle,
        "",
        _row("변경 파일", f"{review.changed_files}개"),
        _row("DB 변경", {"yes": "있음", "no": "없음", "possible": "영향 가능"}[review.database_change]),
        _row(
            "API Contract",
            {"changed": "변경", "unchanged": "변경 없음", "possible": "영향 가능"}[review.api_contract],
        ),
        _row(
            "외부연동",
            {"affected": "영향 있음", "unchanged": "변경 없음", "possible": "영향 가능"}[
                review.external_integration
            ],
        ),
        "",
        "검증된 사실",
        f"{_status_icon(review.django_check)} Django check {_status_text(review.django_check)}",
        f"{_status_icon(review.migration)} Migration check {_migration_text(review.migration)}",
        f"{_status_icon(review.tests.status)} 테스트(pytest) {_test_text(review.tests)}",
        "",
        _row("위험도", {"low": "🟢 LOW", "medium": "🟡 MEDIUM", "high": "🟠 HIGH"}[review.risk]),
        "",
        "주요 변경",
        *[f"• {item}" for item in review.key_changes],
        "",
        "새롭게 생긴 가정",
        *[f"• {item}" for item in review.new_assumptions],
        "",
        "주의",
        *([f"• {item}" for item in review.cautions] or ["• 없음"]),
        "",
        "확인 필요",
        *[f"□ {item}" for item in review.human_checks],
    ]
    marker = f"<!-- gitea-auto-reviewer:pr={pr_number}:sha={head_sha} -->"
    return f"{marker}\n\n```text\n" + "\n".join(lines) + "\n```"


def _row(label: str, value: str) -> str:
    return f"{label}{' ' * max(1, 18 - _display_width(label))}{value}"


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1 for character in value)


def _test_text(result: TestResult) -> str:
    if result.status == "not_run":
        return "NOT RUN"
    return f"{result.passed}/{result.total} {result.status.upper()}"


def _status_icon(status: str) -> str:
    return {"pass": "✅", "no_missing": "✅", "fail": "❌", "missing": "❌",
            "error": "⚠", "not_run": "○"}[status]


def _status_text(status: str) -> str:
    return status.upper().replace("_", " ")


def _migration_text(status: str) -> str:
    return {"no_missing": "누락 없음", "missing": "누락 감지", "error": "ERROR", "not_run": "NOT RUN"}[status]
