"""Compact change-impact validation and rendering."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .messages import text

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
    "impact_paths",
}
IMPACT_KINDS = {"signal", "orm_field", "url", "runtime_caller", "runtime_callee"}
IMPACT_ORIGINS = {"django", "orm", "runtime"}
IMPACT_PATH_FIELDS = ("kind", "source", "target", "detail", "evidence", "origin", "direction", "static_missed")
MAX_RENDERED_IMPACT_PATHS = 12

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
        "impact_paths": {"type": "array", "items": {"$ref": "#/$defs/impactPath"}},
    },
    "$defs": {
        "impactPath": {
            "type": "object", "additionalProperties": False,
            "required": list(IMPACT_PATH_FIELDS),
            "properties": {
                "kind": {"enum": sorted(IMPACT_KINDS)},
                "source": {"type": "string", "maxLength": 300},
                "target": {"type": "string", "maxLength": 300},
                "detail": {"type": "string", "maxLength": 300},
                "evidence": {"type": "string", "maxLength": 300},
                "origin": {"enum": sorted(IMPACT_ORIGINS)},
                "direction": {"enum": ["upstream", "downstream"]},
                "static_missed": {"type": ["boolean", "null"]},
            },
        },
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
    reproduction_status: str | None = None
    reproduction_detail: str | None = None

    @classmethod
    def from_value(cls, value: object) -> Finding:
        required = {"category", "problem", "impact", "evidence", "policy_quote"}
        optional = {"reproduction_status", "reproduction_detail"}
        if not isinstance(value, dict) or not required <= set(value) <= required | optional:
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
        status = value.get("reproduction_status")
        if status not in {None, "unplanned", "inconclusive", "not_reproduced", "verification_rejected"}:
            raise ValueError("invalid finding reproduction status")
        detail = value.get("reproduction_detail")
        if detail is not None:
            detail = _short_text(detail, "reproduction_detail")
        if (status is None) != (detail is None):
            raise ValueError("finding reproduction status and detail must be provided together")
        return cls(category, *strings, _references(value["evidence"], required=True), quote, status, detail)


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
    reached_targets: tuple[str, ...] = ()
    mode: str = "assert"
    predicted_differences: tuple[str, ...] = ()
    other_differences: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: object) -> "ReproducedFinding":
        required = {"problem", "impact", "evidence", "condition", "oracle", "expected", "observed", "cleanup_verified"}
        optional = {"population_label", "matching_count", "total_count", "reached_targets",
                    "mode", "predicted_differences", "other_differences"}
        if (not isinstance(value, dict) or not required <= set(value) <= required | optional
                or value["cleanup_verified"] is not True):
            raise ValueError("invalid reproduced finding")
        population = _population(value.get("population_label"), value.get("matching_count"),
                                 value.get("total_count"))
        reached = value.get("reached_targets", [])
        if not isinstance(reached, (list, tuple)) or any(not isinstance(ref, str) for ref in reached):
            raise ValueError("invalid reproduced target evidence")
        mode = value.get("mode", "assert")
        if mode not in {"assert", "differential"}:
            raise ValueError("invalid reproduced finding mode")
        differences = []
        for name in ("predicted_differences", "other_differences"):
            items = value.get(name, [])
            if (not isinstance(items, (list, tuple)) or len(items) > 10
                    or any(not isinstance(item, str) or len(item) > 300 for item in items)):
                raise ValueError(f"invalid {name}")
            differences.append(tuple(items))
        if mode == "differential" and not differences[0]:
            raise ValueError("differential reproduced finding requires predicted differences")
        return cls(_text(value["problem"], "problem"), _text(value["impact"], "impact"),
                   _references(value["evidence"], True), _text(value["condition"], "condition"),
                   _text(value["oracle"], "oracle"), _short_text(value["expected"], "expected", empty=True),
                   _text(value["observed"], "observed"), True, *population, tuple(reached),
                   mode, *differences)


@dataclass(frozen=True)
class ImpactPath:
    """A deterministic impact edge; static_missed is None when GitNexus was not compared."""

    kind: str
    source: str
    target: str
    detail: str
    evidence: str
    origin: str
    direction: str
    static_missed: bool | None = None


def impact_path(value: object) -> ImpactPath:
    if not isinstance(value, dict) or set(value) != set(IMPACT_PATH_FIELDS):
        raise ValueError("invalid impact path")
    if (value["kind"] not in IMPACT_KINDS or value["origin"] not in IMPACT_ORIGINS
            or value["direction"] not in {"upstream", "downstream"}
            or value["static_missed"] not in {True, False, None}):
        raise ValueError("invalid impact path")
    for name in ("source", "target", "detail", "evidence"):
        if not isinstance(value[name], str) or not value[name].strip() or len(value[name]) > 300:
            raise ValueError("invalid impact path")
    return ImpactPath(**value)


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
    impact_paths: tuple[ImpactPath, ...] = ()

    @classmethod
    def from_json(cls, raw: str) -> Review:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Codex output is not valid JSON") from exc
        # Accept v0.1 stored reviews while the CLI always emits the v0.2 field.
        if isinstance(value, dict) and "reproduced_findings" not in value:
            value["reproduced_findings"] = []
        if isinstance(value, dict) and "impact_paths" not in value:
            value["impact_paths"] = []
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
            impact_paths=_impact_paths(value["impact_paths"]),
        )

    def to_json(self) -> str:
        value = asdict(self)
        for name in ("changed_file_paths", "external_integration_evidence", "risk_evidence", "key_changes", "findings", "reproduced_findings", "affected_files", "impact_paths"):
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


MARKER_PATTERN = re.compile(r"<!-- gitea-auto-reviewer:pr=\d+:sha=([0-9a-f]{40}) -->")
STATE_PATTERN = re.compile(r"<!-- gitea-auto-reviewer-state:([A-Za-z0-9+/=]+) -->")


@dataclass(frozen=True)
class PreviousReview:
    head_sha: str
    reproduced_findings: tuple[ReproducedFinding, ...]
    unreproduced_count: int | None = None
    rejected_count: int | None = None


def parse_previous_review(comment: str | None) -> PreviousReview | None:
    """Decode the hidden state of an earlier bot comment; malformed state is ignored."""
    if not comment:
        return None
    marker = MARKER_PATTERN.search(comment)
    state = STATE_PATTERN.search(comment)
    if not marker or not state:
        return None
    try:
        value = json.loads(base64.b64decode(state.group(1), validate=True).decode("utf-8"))
        if isinstance(value, list):  # state written before finding counts were recorded
            return PreviousReview(marker.group(1), _reproduced_findings(value))
        if not isinstance(value, dict) or set(value) != {"reproduced", "unreproduced", "rejected"} \
                or not _count(value["unreproduced"]) or not _count(value["rejected"]):
            return None
        return PreviousReview(marker.group(1), _reproduced_findings(value["reproduced"]),
                              value["unreproduced"], value["rejected"])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def preserve_reproduced_findings(review: Review, previous_comment: str | None, head_sha: str) -> Review:
    previous = parse_previous_review(previous_comment)
    if previous is None or previous.head_sha != head_sha:
        return review
    merged = tuple(dict.fromkeys((*previous.reproduced_findings, *review.reproduced_findings)))
    return replace(review, reproduced_findings=merged)


def _same_problem(left: ReproducedFinding, right: ReproducedFinding) -> bool:
    # Codex rewords problems between runs, so identical evidence also counts as the same issue.
    return left.problem == right.problem or left.evidence == right.evidence


def _impact_paths(value: object) -> tuple[ImpactPath, ...]:
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError("impact_paths must contain 0-40 items")
    return tuple(impact_path(item) for item in value)


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


def render_markdown(review: Review, pr_number: int, head_sha: str, pr_title: str,
                    previous: PreviousReview | None = None, language: str = "en") -> str:
    def t(key: str, **values: object) -> str:
        return text(language, key, **values)

    title = " ".join(pr_title.split())[:120]
    if previous is not None and previous.head_sha == head_sha:
        previous = None
    new_findings = tuple(
        item for item in review.reproduced_findings
        if previous is not None and not any(_same_problem(item, old) for old in previous.reproduced_findings)
    )
    resolved = tuple(
        item for item in (previous.reproduced_findings if previous else ())
        if not any(_same_problem(item, current) for current in review.reproduced_findings)
    )
    heading = f"PR #{pr_number} {title}"
    reproduced_keys = {(item.problem, item.impact, item.evidence) for item in review.reproduced_findings}
    static_findings = tuple(item for item in review.findings
                            if (item.problem, item.impact, item.evidence) not in reproduced_keys)
    verification_rejected = tuple(
        item for item in static_findings if item.reproduction_status == "verification_rejected"
    )
    unreproduced = tuple(
        item for item in static_findings if item.reproduction_status != "verification_rejected"
    )
    width = max(28, _display_width(heading) + 2)
    top = f"┌{'─' * width}┐"
    middle = f"├{'─' * width}┤"
    heading_line = f"│ {heading}{' ' * (width - _display_width(heading) - 1)}│"
    impact_value = {"yes": "impact_yes", "changed": "impact_changed", "affected": "impact_affected",
                    "possible": "impact_possible", "not_detected": "impact_not_detected"}
    data_value = {**impact_value, "changed": "impact_yes"}
    lines = [
        top,
        heading_line,
        middle,
        "",
        _row(t("changed_files"), t("file_count", count=review.changed_files)),
        *(f"  └ {path}" for path in review.changed_file_paths),
        _row(t("database_change"), t(impact_value[review.database_change])),
        *(f"  └ {item.description} — {', '.join(item.evidence)}" for item in review.database_change_details),
        _row(t("data_change"), t(data_value[review.data_change])),
        *(f"  └ {item.description} — {', '.join(item.evidence)}" for item in review.data_change_details),
        _row(t("api_contract"), t(impact_value[review.api_contract])),
        _row(t("external_integration"), t(impact_value[review.external_integration])),
        *(
            [f"  └ {review.external_integration_reason} — {', '.join(review.external_integration_evidence)}"]
            if review.external_integration_reason else []
        ),
        "",
        t("verified_facts"),
        f"{_status_icon(review.django_check)} Django check {_status_text(review.django_check)}",
        f"{_status_icon(review.migration)} Migration check {_migration_text(review.migration, language)}",
        f"{_status_icon(review.tests.status)} {t('tests')} {_test_text(review.tests)}",
        "",
        _row(t("risk"), {"low": "🟢 LOW", "medium": "🟡 MEDIUM", "high": "🟠 HIGH"}[review.risk]
             + f" · {t('risk_basis')} {review.risk_confidence.upper()}"),
        *[f"  └ {reference}" for reference in review.risk_evidence],
        "",
        t("key_changes"),
        *[f"• {item}" for item in review.key_changes],
        "",
        *(_reproduced_finding_section(review.reproduced_findings, new_findings, language)
          if review.reproduced_findings else ["", t("reproduced"), f"  • {t('no_reproduced')}"]),
        *(_previous_finding_section(resolved, previous.head_sha, language) if previous and resolved else []),
        *(_finding_section(unreproduced, t("unreproduced"), t("unreproduced_note"), language)
          if unreproduced else []),
        *(_finding_section(verification_rejected, t("rejected"), t("rejected_note"), language)
          if verification_rejected else []),
        *(_impact_path_section(review.impact_paths, language) if review.impact_paths else []),
        *(_affected_file_section(review.affected_files, language) if review.affected_files else []),
    ]
    marker = f"<!-- gitea-auto-reviewer:pr={pr_number}:sha={head_sha} -->"
    state = base64.b64encode(json.dumps({
        "reproduced": [asdict(item) for item in review.reproduced_findings],
        "unreproduced": len(unreproduced),
        "rejected": len(verification_rejected),
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{marker}\n<!-- gitea-auto-reviewer-state:{state} -->\n\n```text\n" + "\n".join(lines).rstrip() + "\n```"


def _evidence_paths(evidence: tuple[str, ...]) -> list[str]:
    return [f"    └ {path}" for path in dict.fromkeys(ref.rpartition(":")[0] for ref in evidence)]


def _finding_section(findings: tuple[Finding, ...], title: str, note: str, language: str) -> list[str]:
    lines = [title, note]
    for finding in findings:
        if len(lines) > 2:
            lines.append("")
        lines.append(f"  • {finding.problem}")
        lines.append(f"    {text(language, 'impact')}: {finding.impact}")
        if finding.reproduction_status == "verification_rejected":
            lines.append(f"    {text(language, 'rejection_reason')}: {finding.reproduction_detail}")
        elif finding.reproduction_status:
            label = text(language, f"status_{finding.reproduction_status}")
            lines.append(f"    {text(language, 'reproduction_result')}: {label} — {finding.reproduction_detail}")
        if finding.policy_quote:
            lines.append(f'    {text(language, "policy_rule")}: "{finding.policy_quote}"')
        lines.extend(_evidence_paths(finding.evidence))
    return ["", *lines]


def _reproduced_finding_section(findings: tuple[ReproducedFinding, ...],
                                new_findings: tuple[ReproducedFinding, ...] = (),
                                language: str = "en") -> list[str]:
    def t(key: str, **values: object) -> str:
        return text(language, key, **values)

    lines = [t("reproduced")]
    for finding in findings:
        if len(lines) > 1:
            lines.append("")
        lines.append(f"  • {t('new_marker') if finding in new_findings else ''}{finding.problem}")
        lines.append(f"    {t('impact')}: {finding.impact}")
        lines.append(f"    {t('conditions')}")
        lines.extend(f"      {index}. {condition}" for index, condition in enumerate(
            (line.strip() for line in finding.condition.splitlines() if line.strip()), start=1
        ))
        if (finding.population_label is not None and finding.matching_count is not None
                and finding.total_count is not None):
            rate = finding.matching_count / finding.total_count * 100
            lines.append(
                f"    {t('condition_rate')}: {finding.population_label} "
                f"{finding.matching_count:,}/{finding.total_count:,}{t('rows')} ({rate:.2f}%)"
            )
        if finding.mode == "differential":
            lines.append(f"    {t('differential_runs')}")
            lines.extend(f"      {line}" for line in finding.predicted_differences)
            if finding.other_differences:
                lines.append(f"    {t('other_differences')}")
                lines.extend(f"      {line}" for line in finding.other_differences)
        else:
            lines.append(f"    {t('observed')}: {finding.observed}")
        if finding.reached_targets:
            lines.append(f"    {t('reached')}: {', '.join(finding.reached_targets)}")
        lines.append(f"    {t('rollback_passed')}")
        lines.extend(_evidence_paths(finding.evidence))
    return ["", *lines]


def _previous_finding_section(findings: tuple[ReproducedFinding, ...], head_sha: str,
                              language: str = "en") -> list[str]:
    lines = ["", text(language, "previous_commit", sha=head_sha[:7]), text(language, "previous_commit_note")]
    for finding in findings:
        if len(lines) > 3:
            lines.append("")
        lines.append(f"  • {finding.problem}")
        lines.append(f"    {text(language, 'impact')}: {finding.impact}")
        lines.extend(_evidence_paths(finding.evidence))
    return lines


def _impact_path_section(paths: tuple[ImpactPath, ...], language: str = "en") -> list[str]:
    ordered = sorted(paths, key=lambda item: (item.static_missed is not True, item.kind, item.source, item.target))
    shown = ordered[:MAX_RENDERED_IMPACT_PATHS]
    compared = any(item.static_missed is not None for item in paths)
    lines = ["", text(language, "impact_paths"),
             text(language, "impact_paths_note") + (text(language, "impact_paths_missed") if compared else "")]
    for item in shown:
        marker = " ⚠" if item.static_missed else ""
        lines.append(f"  • [{text(language, f'path_{item.kind}')}] {item.source} → {item.target}{marker}")
        lines.append(f"      {item.detail} — {item.evidence}")
    if len(ordered) > len(shown):
        lines.append(f"  • {text(language, 'more_paths', count=len(ordered) - len(shown))}")
    return lines


def _affected_file_section(files: tuple[AffectedFile, ...], language: str = "en") -> list[str]:
    lines = ["", text(language, "affected_files")]
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


def _migration_text(status: str, language: str = "en") -> str:
    if status in {"no_missing", "missing"}:
        return text(language, f"migration_{status}")
    return {"error": "ERROR", "not_run": "NOT RUN"}[status]
