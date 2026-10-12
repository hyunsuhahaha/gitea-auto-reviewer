import json
import re
import string

import pytest

from gitea_auto_reviewer.differential import compare_runs
from gitea_auto_reviewer.messages import LANGUAGE_NAMES, MESSAGES, language_name, text
from gitea_auto_reviewer.reproduction import (ReproductionEvidence, ReproductionResult, VerificationDecision,
                                              build_plan_prompt, finalize_review)
from gitea_auto_reviewer.review import Review, render_markdown
from test_review import impact_payload

HANGUL = re.compile("[가-힣]")
SHA = "a" * 40


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_every_message_exists_in_every_language_with_the_same_placeholders(key) -> None:
    assert set(MESSAGES[key]) == set(LANGUAGE_NAMES)
    assert len({frozenset(_placeholders(value)) for value in MESSAGES[key].values()}) == 1
    assert not HANGUL.search(MESSAGES[key]["en"])


def test_unknown_language_is_rejected() -> None:
    with pytest.raises(ValueError):
        text("fr", "risk")
    with pytest.raises(ValueError):
        language_name("fr")


def _reviewed() -> Review:
    payload = impact_payload()
    finding = payload["findings"][0]
    payload["reproduced_findings"] = [{
        "problem": "Existing create path omits remark", "impact": "Product creation fails",
        "evidence": finding["evidence"], "condition": "call the old create path\nwithout remark",
        "oracle": "creation succeeds", "expected": "created", "observed": "required field error",
        "cleanup_verified": True, "population_label": "products", "matching_count": 4, "total_count": 12,
    }]
    return Review.from_json(json.dumps(payload))


def test_english_comment_has_no_korean_labels() -> None:
    review = _reviewed()
    review = Review.from_json(json.dumps({
        **json.loads(review.to_json()),
        "changed_file_paths": ["a.py"], "changed_files": 1, "key_changes": ["Adds Product.remark"],
        "database_change_details": [{"description": "adds product.remark", "evidence": ["a.py:1"]}],
        "data_change_details": [{"description": "returns remark", "evidence": ["a.py:2"]}],
        "external_integration_reason": "SCM sync creates products",
        "findings": [{**json.loads(review.to_json())["findings"][0], "problem": "Static only",
                      "impact": "Might fail", "reproduction_status": "unplanned",
                      "reproduction_detail": "not planned"}],
        "affected_files": [{"path": "app/admin.py", "reason": "creates products", "evidence": ["app/admin.py:5"]}],
    }))

    rendered = render_markdown(review, 7, SHA, "Add remark", language="en")

    body = rendered.split("```text\n", 1)[1]
    assert not HANGUL.search(body), HANGUL.findall(body)
    assert "Changed files" in body and "Reproduced problems" in body
    assert "Bug condition rate: products 4/12 rows (33.33%)" in body
    assert "Reproduction: not run — not planned" in body
    assert "Rollback check: passed" in body


def test_render_defaults_to_korean() -> None:
    assert "변경 파일" in render_markdown(_reviewed(), 1, SHA, "t")


def test_english_finalize_and_comparison_text() -> None:
    review = _reviewed()
    final = finalize_review(review, ReproductionEvidence(SHA, ()), language="en")
    assert final.findings[0].reproduction_detail == text("en", "detail_unplanned")

    base = {"exception": None, "result": 1, "changes": []}
    head = {"exception": None, "result": 2, "changes": []}
    comparison = compare_runs([base, base], [head, head], ("result",), language="en")
    assert comparison.status == "confirmed"
    assert comparison.summary.startswith("predicted difference from base observed: return value")


def test_english_verification_requires_concrete_english_reason() -> None:
    evidence = ReproductionEvidence(SHA, (
        ReproductionResult(0, "confirmed", "c", "o", "x", "y", True, 0.1, target_reached=True),
    ))

    def decision(reason: str, language: str) -> VerificationDecision:
        return VerificationDecision.from_json(json.dumps({
            "version": 1, "head_sha": SHA, "accepted_finding_indices": [],
            "rejected_findings": [{"finding_index": 0, "reason": reason}],
        }), evidence, language)

    assert decision("The observed value comes from fixture setup, not the target call.", "en")
    with pytest.raises(ValueError):
        decision("Insufficient evidence.", "en")
    with pytest.raises(ValueError):
        decision("The observed value comes from fixture setup.", "ko")


def test_plan_prompt_asks_for_the_configured_language() -> None:
    review = _reviewed()
    assert "`oracle` in English" in build_plan_prompt(review, SHA, None, "en")
    assert "`oracle` in Korean" in build_plan_prompt(review, SHA, None, "ko")
