"""Compact change-impact validation and rendering."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FIELDS = {
    "changed_files",
    "database_change",
    "django_check",
    "migration",
    "api_contract",
    "external_integration",
    "external_integration_reason",
    "external_integration_evidence",
    "tests",
    "risk",
    "risk_confidence",
    "risk_evidence",
    "key_changes",
    "findings",
}

REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": sorted(FIELDS),
    "properties": {
        "changed_files": {"type": "integer", "minimum": 1},
        "database_change": {"enum": ["yes", "possible", "not_detected"]},
        "django_check": {"enum": ["pass", "fail", "error", "not_run"]},
        "migration": {"enum": ["no_missing", "missing", "error", "not_run"]},
        "api_contract": {"enum": ["changed", "possible", "not_detected"]},
        "external_integration": {"enum": ["affected", "possible", "not_detected"]},
        "external_integration_reason": {"type": ["string", "null"], "maxLength": 500},
        "external_integration_evidence": {"$ref": "#/$defs/references"},
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
        "risk_confidence": {"enum": ["low", "medium", "high"]},
        "risk_evidence": {"$ref": "#/$defs/references"},
        "key_changes": {"$ref": "#/$defs/requiredItems"},
        "findings": {"$ref": "#/$defs/findings"},
    },
    "$defs": {
        "requiredItems": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "references": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "pattern": "^.+:[1-9][0-9]*$", "maxLength": 300},
        },
        "requiredReferences": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "pattern": "^.+:[1-9][0-9]*$", "maxLength": 300},
        },
        "findings": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "problem", "impact", "change", "expected_state", "evidence", "policy_quote"],
                "properties": {
                    "category": {"enum": ["bug", "security", "performance", "policy"]},
                    "problem": {"type": "string", "minLength": 1, "maxLength": 500},
                    "impact": {"type": "string", "minLength": 1, "maxLength": 500},
                    "change": {"type": "string", "minLength": 1, "maxLength": 500},
                    "expected_state": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence": {"$ref": "#/$defs/requiredReferences"},
                    "policy_quote": {"type": ["string", "null"], "maxLength": 500},
                },
            },
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
class Finding:
    category: str
    problem: str
    impact: str
    change: str
    expected_state: str
    evidence: tuple[str, ...]
    policy_quote: str | None

    @classmethod
    def from_value(cls, value: object) -> Finding:
        names = {"category", "problem", "impact", "change", "expected_state", "evidence", "policy_quote"}
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError("finding does not match the required schema")
        category = value["category"]
        if category not in {"bug", "security", "performance", "policy"}:
            raise ValueError("invalid finding category")
        strings = [_text(value[name], name) for name in ("problem", "impact", "change", "expected_state")]
        quote = value["policy_quote"]
        if category == "policy":
            quote = _text(quote, "policy_quote")
        elif quote is not None:
            raise ValueError("policy_quote is only allowed for policy findings")
        return cls(category, *strings, _references(value["evidence"], required=True), quote)


@dataclass(frozen=True)
class Review:
    changed_files: int
    database_change: str
    django_check: str
    migration: str
    api_contract: str
    external_integration: str
    external_integration_reason: str | None
    external_integration_evidence: tuple[str, ...]
    tests: TestResult
    risk: str
    risk_confidence: str
    risk_evidence: tuple[str, ...]
    key_changes: tuple[str, ...]
    findings: tuple[Finding, ...]

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
        _enum(value, "database_change", {"yes", "possible", "not_detected"})
        _enum(value, "django_check", {"pass", "fail", "error", "not_run"})
        _enum(value, "migration", {"no_missing", "missing", "error", "not_run"})
        _enum(value, "api_contract", {"changed", "possible", "not_detected"})
        _enum(value, "external_integration", {"affected", "possible", "not_detected"})
        _enum(value, "risk", {"low", "medium", "high"})
        _enum(value, "risk_confidence", {"low", "medium", "high"})
        risk_evidence = _references(value["risk_evidence"], required=value["risk"] != "low")
        external_reason = value["external_integration_reason"]
        external_evidence = _references(
            value["external_integration_evidence"], required=value["external_integration"] != "not_detected"
        )
        if value["external_integration"] == "not_detected":
            if external_reason is not None or external_evidence:
                raise ValueError("not_detected external integration must not include a reason")
        else:
            external_reason = _text(external_reason, "external_integration_reason")
        return cls(
            changed_files=changed_files,
            database_change=value["database_change"],
            django_check=value["django_check"],
            migration=value["migration"],
            api_contract=value["api_contract"],
            external_integration=value["external_integration"],
            external_integration_reason=external_reason,
            external_integration_evidence=external_evidence,
            tests=TestResult.from_value(value["tests"]),
            risk=value["risk"],
            risk_confidence=value["risk_confidence"],
            risk_evidence=risk_evidence,
            key_changes=_items(value["key_changes"], "key_changes", required=True),
            findings=_findings(value["findings"]),
        )

    def to_json(self) -> str:
        value = asdict(self)
        for name in ("external_integration_evidence", "risk_evidence", "key_changes", "findings"):
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


def _findings(value: object) -> tuple[Finding, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("findings must contain 0-5 items")
    return tuple(Finding.from_value(item) for item in value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"invalid {name}")
    return value.strip()


def _references(value: object, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 5 or (required and not value):
        raise ValueError("evidence must contain file:line references")
    references: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 300:
            raise ValueError("invalid evidence reference")
        path, separator, line = item.strip().rpartition(":")
        if not separator or not path or not line.isdigit() or int(line) < 1 or "\n" in path:
            raise ValueError("evidence must use repo/path:line")
        references.append(f"{path}:{line}")
    return tuple(references)


def validate_grounding(review: Review, repository: Path, policy: str) -> None:
    """Reject fabricated locations and policy quotations before publication."""
    root = repository.resolve()
    for reference in (
        *review.external_integration_evidence,
        *review.risk_evidence,
        *(ref for item in review.findings for ref in item.evidence),
    ):
        relative, _, line_text = reference.rpartition(":")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"evidence escapes repository: {reference}") from exc
        if not candidate.is_file():
            raise ValueError(f"evidence file does not exist: {reference}")
        with candidate.open(encoding="utf-8", errors="replace") as source:
            if sum(1 for _ in source) < int(line_text):
                raise ValueError(f"evidence line does not exist: {reference}")
    for finding in review.findings:
        if finding.category == "policy" and finding.policy_quote not in policy:
            raise ValueError("policy finding does not quote the base AI_REVIEW.md exactly")


def validate_verification(draft: Review, verified: Review) -> None:
    """The rejection pass may remove findings, never invent or rewrite them."""
    if any(finding not in draft.findings for finding in verified.findings):
        raise ValueError("verification introduced or rewrote a finding")
    draft_external = (
        draft.external_integration,
        draft.external_integration_reason,
        draft.external_integration_evidence,
    )
    verified_external = (
        verified.external_integration,
        verified.external_integration_reason,
        verified.external_integration_evidence,
    )
    if verified_external != draft_external and verified_external != ("not_detected", None, ()):
        raise ValueError("verification introduced or rewrote external integration impact")


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
        _row("DB 변경", {"yes": "있음", "not_detected": "직접 영향 미발견", "possible": "영향 가능"}[review.database_change]),
        _row(
            "API Contract",
            {"changed": "변경", "not_detected": "직접 영향 미발견", "possible": "영향 가능"}[review.api_contract],
        ),
        _row(
            "외부연동",
            {"affected": "영향 있음", "not_detected": "직접 영향 미발견", "possible": "영향 가능"}[
                review.external_integration
            ],
        ),
        *(
            [f"  └ {review.external_integration_reason} — {', '.join(review.external_integration_evidence)}"]
            if review.external_integration_reason else []
        ),
        "",
        "검증된 사실",
        f"{_status_icon(review.django_check)} Django check {_status_text(review.django_check)}",
        f"{_status_icon(review.migration)} Migration check {_migration_text(review.migration)}",
        f"{_status_icon(review.tests.status)} 테스트(pytest) {_test_text(review.tests)}",
        "",
        _row("위험도", {"low": "🟢 LOW", "medium": "🟡 MEDIUM", "high": "🟠 HIGH"}[review.risk]
             + f" · 근거 {review.risk_confidence.upper()}"),
        *[f"  └ {reference}" for reference in review.risk_evidence],
        "",
        "주요 변경",
        *[f"• {item}" for item in review.key_changes],
        "",
        *(_finding_section(review.findings) if review.findings else []),
    ]
    marker = f"<!-- gitea-auto-reviewer:pr={pr_number}:sha={head_sha} -->"
    return (f"{marker}\n\n```text\n" + "\n".join(lines).rstrip() +
            "\n```\n\n이 리뷰가 유용했다면 👍, 노이즈였다면 👎 반응을 남겨주세요.")


def _finding_section(findings: tuple[Finding, ...]) -> list[str]:
    lines = ["주의"]
    for finding in findings:
        lines.append(f"• {finding.problem}")
        lines.append(f"  영향: {finding.impact}")
        lines.append(f"  수정: {finding.change}")
        lines.append(f"  완료: {finding.expected_state}")
        if finding.policy_quote:
            lines.append(f'  규칙: "{finding.policy_quote}"')
        lines.extend(f"  └ {reference}" for reference in finding.evidence)
    return ["", *lines]


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
