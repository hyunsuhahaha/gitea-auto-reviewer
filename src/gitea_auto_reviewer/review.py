"""Compact change-impact validation and rendering."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

FIELDS = {
    "changed_files",
    "changed_file_paths",
    "database_change",
    "database_change_details",
    "data_change",
    "data_change_details",
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
    "reproduced_findings",
    "affected_files",
}

REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": sorted(FIELDS),
    "properties": {
        "changed_files": {"type": "integer", "minimum": 1},
        "changed_file_paths": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "database_change": {"enum": ["yes", "possible", "not_detected"]},
        "database_change_details": {"$ref": "#/$defs/impactDetails"},
        "data_change": {"enum": ["changed", "possible", "not_detected"]},
        "data_change_details": {"$ref": "#/$defs/impactDetails"},
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
        "reproduced_findings": {"type": "array", "items": {"$ref": "#/$defs/reproducedFinding"}},
        "affected_files": {"$ref": "#/$defs/affectedFiles"},
    },
    "$defs": {
        "requiredItems": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
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
        "impactDetails": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "evidence"],
                "properties": {
                    "description": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence": {"$ref": "#/$defs/requiredReferences"},
                },
            },
        },
        "findings": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "problem", "impact", "evidence", "policy_quote"],
                "properties": {
                    "category": {"enum": ["bug", "security", "performance", "dependency", "policy"]},
                    "problem": {"type": "string", "minLength": 1, "maxLength": 500},
                    "impact": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence": {"$ref": "#/$defs/requiredReferences"},
                    "policy_quote": {"type": ["string", "null"], "maxLength": 500},
                },
            },
        },
        "affectedFiles": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "reason", "evidence"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 500},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence": {"$ref": "#/$defs/requiredReferences"},
                },
            },
        },
        "reproducedFinding": {
            "type": "object", "additionalProperties": False,
            "required": [
                "problem", "impact", "evidence", "condition", "oracle", "expected", "observed",
                "cleanup_verified", "population_label", "matching_count", "total_count",
            ],
            "properties": {
                "problem": {"type": "string", "minLength": 1, "maxLength": 500},
                "impact": {"type": "string", "minLength": 1, "maxLength": 500},
                "evidence": {"$ref": "#/$defs/requiredReferences"},
                "condition": {"type": "string", "minLength": 1, "maxLength": 1000},
                "oracle": {"type": "string", "minLength": 1, "maxLength": 1000},
                "expected": {"type": "string", "maxLength": 1000},
                "observed": {"type": "string", "minLength": 1, "maxLength": 1000},
                "cleanup_verified": {"type": "boolean", "const": True},
                "population_label": {"type": ["string", "null"], "maxLength": 200},
                "matching_count": {"type": ["integer", "null"], "minimum": 0},
                "total_count": {"type": ["integer", "null"], "minimum": 1},
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
    evidence: tuple[str, ...]
    policy_quote: str | None

    @classmethod
    def from_value(cls, value: object) -> Finding:
        names = {"category", "problem", "impact", "evidence", "policy_quote"}
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError("finding does not match the required schema")
        category = value["category"]
        if category not in {"bug", "security", "performance", "dependency", "policy"}:
            raise ValueError("invalid finding category")
        strings = [_text(value[name], name) for name in ("problem", "impact")]
        quote = value["policy_quote"]
        if category == "policy":
            quote = _text(quote, "policy_quote")
        elif quote is not None:
            raise ValueError("policy_quote is only allowed for policy findings")
        return cls(category, *strings, _references(value["evidence"], required=True), quote)


@dataclass(frozen=True)
class ImpactDetail:
    description: str
    evidence: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> ImpactDetail:
        if not isinstance(value, dict) or set(value) != {"description", "evidence"}:
            raise ValueError("impact detail does not match the required schema")
        return cls(_text(value["description"], "description"), _references(value["evidence"], required=True))


@dataclass(frozen=True)
class AffectedFile:
    path: str
    reason: str
    evidence: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> AffectedFile:
        if not isinstance(value, dict) or set(value) != {"path", "reason", "evidence"}:
            raise ValueError("affected file does not match the required schema")
        path = _path(value["path"], "affected file path")
        return cls(path, _text(value["reason"], "affected file reason"), _references(value["evidence"], True))


@dataclass(frozen=True)
class ReproducedFinding:
    problem: str
    impact: str
    evidence: tuple[str, ...]
    condition: str
    oracle: str
    expected: str
    observed: str
    cleanup_verified: bool
    population_label: str | None = None
    matching_count: int | None = None
    total_count: int | None = None

    @classmethod
    def from_value(cls, value: object) -> "ReproducedFinding":
        required = {"problem", "impact", "evidence", "condition", "oracle", "expected", "observed", "cleanup_verified"}
        optional = {"population_label", "matching_count", "total_count"}
        if (not isinstance(value, dict) or not required <= set(value) <= required | optional
                or value["cleanup_verified"] is not True):
            raise ValueError("invalid reproduced finding")
        population = _population(value.get("population_label"), value.get("matching_count"),
                                 value.get("total_count"))
        return cls(_text(value["problem"], "problem"), _text(value["impact"], "impact"),
                   _references(value["evidence"], True), _text(value["condition"], "condition"),
                   _text(value["oracle"], "oracle"), _short_text(value["expected"], "expected", empty=True),
                   _text(value["observed"], "observed"), True, *population)


@dataclass(frozen=True)
class Review:
    changed_files: int
    changed_file_paths: tuple[str, ...]
    database_change: str
    database_change_details: tuple[ImpactDetail, ...]
    data_change: str
    data_change_details: tuple[ImpactDetail, ...]
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
    reproduced_findings: tuple[ReproducedFinding, ...]
    affected_files: tuple[AffectedFile, ...]

    @classmethod
    def from_json(cls, raw: str) -> Review:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Codex output is not valid JSON") from exc
        # Accept v0.1 stored reviews while the CLI always emits the v0.2 field.
        if isinstance(value, dict) and "reproduced_findings" not in value:
            value["reproduced_findings"] = []
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("review output does not match the change-impact schema")
        changed_files = value["changed_files"]
        if not _count(changed_files) or changed_files < 1:
            raise ValueError("changed_files must be a positive integer")
        changed_file_paths = _paths(value["changed_file_paths"])
        if len(changed_file_paths) != changed_files:
            raise ValueError("changed file count does not match changed_file_paths")
        _enum(value, "database_change", {"yes", "possible", "not_detected"})
        database_details = _impact_details(value["database_change_details"])
        if value["database_change"] == "not_detected" and database_details:
            raise ValueError("not_detected database change must not include details")
        if value["database_change"] != "not_detected" and not database_details:
            raise ValueError("database change requires concrete details")
        _enum(value, "data_change", {"changed", "possible", "not_detected"})
        data_details = _impact_details(value["data_change_details"])
        if value["data_change"] == "not_detected" and data_details:
            raise ValueError("not_detected data change must not include details")
        if value["data_change"] != "not_detected" and not data_details:
            raise ValueError("data change requires concrete details")
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
            changed_file_paths=changed_file_paths,
            database_change=value["database_change"],
            database_change_details=database_details,
            data_change=value["data_change"],
            data_change_details=data_details,
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
            reproduced_findings=_reproduced_findings(value["reproduced_findings"]),
            affected_files=_affected_files(value["affected_files"], changed_file_paths),
        )

    def to_json(self) -> str:
        value = asdict(self)
        for name in ("changed_file_paths", "external_integration_evidence", "risk_evidence", "key_changes", "findings", "reproduced_findings", "affected_files"):
            value[name] = list(value[name])
        return json.dumps(value, ensure_ascii=False, indent=2)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _population(label: object, matching: object, total: object) -> tuple[str | None, int | None, int | None]:
    if (not isinstance(label, str) or not label.strip() or len(label.strip()) > 200
            or not _count(matching) or not _count(total) or total == 0 or matching > total):
        return None, None, None
    return label.strip(), matching, total


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("changed_file_paths must not be empty")
    paths: list[str] = []
    for item in value:
        paths.append(_path(item, "changed file path"))
    return tuple(paths)


def _path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500 or "\n" in value:
        raise ValueError(f"invalid {name}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid {name}")
    return value


def _enum(value: dict[str, object], name: str, allowed: set[str]) -> None:
    if value[name] not in allowed:
        raise ValueError(f"invalid {name}")


def _items(value: object, name: str, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 10 or (required and not value):
        raise ValueError(f"{name} must contain {'1-10' if required else '0-10'} items")
    items = tuple(item.strip() for item in value if isinstance(item, str))
    if len(items) != len(value) or any(not item or len(item) > 500 for item in items):
        raise ValueError(f"{name} contains an invalid item")
    return items


def _findings(value: object) -> tuple[Finding, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("findings must contain 0-5 items")
    return tuple(Finding.from_value(item) for item in value)


def _reproduced_findings(value: object) -> tuple[ReproducedFinding, ...]:
    if not isinstance(value, list):
        raise ValueError("reproduced_findings must be a list")
    return tuple(ReproducedFinding.from_value(item) for item in value)


def preserve_reproduced_findings(review: Review, previous_comment: str | None, head_sha: str) -> Review:
    if not previous_comment or f"<!-- gitea-auto-reviewer:pr=" not in previous_comment \
            or f":sha={head_sha} -->" not in previous_comment:
        return review
    match = re.search(r"<!-- gitea-auto-reviewer-state:([A-Za-z0-9+/=]+) -->", previous_comment)
    if not match:
        return review
    try:
        value = json.loads(base64.b64decode(match.group(1), validate=True).decode("utf-8"))
        previous = _reproduced_findings(value)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return review
    merged = tuple(dict.fromkeys((*previous, *review.reproduced_findings)))
    return replace(review, reproduced_findings=merged)


def _impact_details(value: object) -> tuple[ImpactDetail, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("impact details must contain 0-5 items")
    return tuple(ImpactDetail.from_value(item) for item in value)


def _affected_files(value: object, changed_paths: tuple[str, ...]) -> tuple[AffectedFile, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("affected_files must contain 0-5 items")
    items = tuple(AffectedFile.from_value(item) for item in value)
    paths = tuple(item.path for item in items)
    if len(set(paths)) != len(paths) or any(path in changed_paths for path in paths):
        raise ValueError("affected_files must be unique and exclude changed files")
    if any(not any(ref.rpartition(":")[0] == item.path for ref in item.evidence) for item in items):
        raise ValueError("each affected file requires evidence from that file")
    return items


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"invalid {name}")
    return value.strip()


def _short_text(value: object, name: str, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 1000 or (not empty and not value.strip()):
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
        *(ref for item in review.database_change_details for ref in item.evidence),
        *(ref for item in review.data_change_details for ref in item.evidence),
        *review.risk_evidence,
        *(ref for item in review.findings for ref in item.evidence),
        *(ref for item in review.reproduced_findings for ref in item.evidence),
        *(ref for item in review.affected_files for ref in item.evidence),
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
    draft_database = (draft.database_change, draft.database_change_details)
    verified_database = (verified.database_change, verified.database_change_details)
    if verified_database != draft_database and verified_database != ("not_detected", ()):
        raise ValueError("verification introduced or rewrote database impact")
    draft_data = (draft.data_change, draft.data_change_details)
    verified_data = (verified.data_change, verified.data_change_details)
    if verified_data != draft_data and verified_data != ("not_detected", ()):
        raise ValueError("verification introduced or rewrote data-processing impact")
    if any(item not in draft.affected_files for item in verified.affected_files):
        raise ValueError("verification introduced or rewrote an affected file")


def render_markdown(review: Review, pr_number: int, head_sha: str, pr_title: str) -> str:
    title = " ".join(pr_title.split())[:120]
    heading = f"PR #{pr_number} {title}"
    reproduced_keys = {(item.problem, item.impact, item.evidence) for item in review.reproduced_findings}
    static_findings = tuple(item for item in review.findings
                            if (item.problem, item.impact, item.evidence) not in reproduced_keys)
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
        *(f"  └ {path}" for path in review.changed_file_paths),
        _row("DB 스키마 변경", {"yes": "있음", "not_detected": "직접 영향 미발견", "possible": "영향 가능"}[review.database_change]),
        *(f"  └ {item.description} — {', '.join(item.evidence)}" for item in review.database_change_details),
        _row("데이터 처리 변경", {"changed": "있음", "not_detected": "직접 영향 미발견", "possible": "영향 가능"}[review.data_change]),
        *(f"  └ {item.description} — {', '.join(item.evidence)}" for item in review.data_change_details),
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
        *(_reproduced_finding_section(review.reproduced_findings)
          if review.reproduced_findings else ["", "재현된 문제", "• 자동 재현 및 2차 검증을 통과"]),
        *(_finding_section(static_findings) if static_findings else []),
        *(_affected_file_section(review.affected_files) if review.affected_files else []),
    ]
    marker = f"<!-- gitea-auto-reviewer:pr={pr_number}:sha={head_sha} -->"
    state = base64.b64encode(json.dumps(
        [asdict(item) for item in review.reproduced_findings], ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).decode("ascii")
    return f"{marker}\n<!-- gitea-auto-reviewer-state:{state} -->\n\n```text\n" + "\n".join(lines).rstrip() + "\n```"


def _finding_section(findings: tuple[Finding, ...]) -> list[str]:
    lines = ["정적 분석 발견 사항(미재현)",
             "※ GitNexus 의존성 그래프와 저장소 코드에 근거하며 DB 재현으로 확정되지 않음"]
    for finding in findings:
        lines.append(f"• {finding.problem}")
        lines.append(f"  영향: {finding.impact}")
        if finding.policy_quote:
            lines.append(f'  규칙: "{finding.policy_quote}"')
        lines.extend(f"  └ {path}" for path in dict.fromkeys(ref.rpartition(":")[0] for ref in finding.evidence))
    return ["", *lines]


def _reproduced_finding_section(findings: tuple[ReproducedFinding, ...]) -> list[str]:
    lines = ["재현된 문제"]
    for finding in findings:
        lines.append(f"• {finding.problem}")
        lines.append(f"  영향: {finding.impact}")
        lines.append("  재현에 사용한 조건")
        lines.extend(f"  {index}. {condition}" for index, condition in enumerate(
            (line.strip() for line in finding.condition.splitlines() if line.strip()), start=1
        ))
        if (finding.population_label is not None and finding.matching_count is not None
                and finding.total_count is not None):
            rate = finding.matching_count / finding.total_count * 100
            lines.append(
                f"  버그 조건 충족률: {finding.population_label} "
                f"{finding.matching_count:,}/{finding.total_count:,}건 ({rate:.2f}%)"
            )
        lines.append(f"  관찰 결과: {finding.observed}")
        lines.append("  롤백 검증: 통과")
        lines.extend(f"  └ {path}" for path in dict.fromkeys(ref.rpartition(":")[0] for ref in finding.evidence))
    return ["", *lines]


def _affected_file_section(files: tuple[AffectedFile, ...]) -> list[str]:
    lines = ["", "영향 파일"]
    for item in files:
        lines.append(f"• {item.path}")
        lines.append(f"  └ {item.reason} — {', '.join(item.evidence)}")
    return lines


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
