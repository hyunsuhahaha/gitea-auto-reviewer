import json
import sqlite3
import sys

import pytest

from gitea_auto_reviewer.differential import (
    base_worktree,
    compare_runs,
    differential_blocker,
    validate_predictions,
)
from gitea_auto_reviewer.reproduction import (
    PLAN_SCHEMA,
    ReproductionCase,
    ReproductionPlan,
    build_plan_prompt,
    finalize_review,
    run_reproductions,
)
from gitea_auto_reviewer.review import Review, render_markdown
from shop_fixture import _git, make_shop_repository
from test_review import impact_payload

ORDER_SCRIPT = """from shop.models import Product
from shop.services import place_order


def reproduce():
    product = Product.objects.get(name="widget")
    order = place_order(product.pk, {quantity})
    return {{"observed": {{"quantity": order.quantity}}}}
"""


def outcome(result=None, exception=None, changes=()):
    return {"result": result, "exception": exception, "changes": list(changes)}


def stock_update(before, after, pk=1):
    return {"model": "shop.Product", "alias": "default", "partial": False, "pk": "id",
            "foreign_keys": {}, "volatile": [], "created": [], "deleted": [],
            "updated": [{"pk": pk, "before": {"id": pk, "stock": before}, "after": {"id": pk, "stock": after}}]}


def created_orders(*rows):
    return [
        {"model": "shop.Product", "alias": "default", "partial": False, "pk": "id",
         "foreign_keys": {}, "volatile": [], "created": [{"id": 7, "stock": 1}],
         "deleted": [], "updated": []},
        {"model": "shop.Order", "alias": "default", "partial": False, "pk": "id",
         "foreign_keys": {"product_id": "shop.Product"}, "volatile": ["created_at"],
         "created": list(rows), "deleted": [], "updated": []},
    ]


def test_compare_runs_without_differences_is_refuted() -> None:
    run = outcome({"quantity": 1}, changes=[stock_update(1, 0)])

    comparison = compare_runs([run, run], [run, run], ("Product.stock",))

    assert comparison.status == "refuted"
    assert comparison.predicted == () and comparison.unpredicted == ()


def test_compare_runs_with_predicted_stock_difference_is_confirmed() -> None:
    base = outcome(exception="OutOfStock: widget")
    head = outcome({"quantity": 3}, changes=[stock_update(1, -2)])

    comparison = compare_runs([base, base], [head, head], ("Product.stock",))

    assert comparison.status == "confirmed"
    assert "Product(pk=1).stock  base 변경 없음 · head 1 → -2" in comparison.predicted
    assert any(line.startswith("예외  base OutOfStock: widget") for line in comparison.unpredicted)


def test_compare_runs_with_only_unpredicted_difference_is_refuted_with_details() -> None:
    base = outcome({"quantity": 1}, changes=[stock_update(1, 0)])
    head = outcome({"quantity": 2}, changes=[stock_update(1, 0)])

    comparison = compare_runs([base, base], [head, head], ("Product.stock",))

    assert comparison.status == "refuted"
    assert comparison.unpredicted == ('반환값  base {"quantity": 1} · head {"quantity": 2}',)
    assert comparison.summary.startswith("예측한 위치의 차이는 없고 다른 차이만 관찰")


def test_compare_runs_ignores_values_that_differ_between_identical_runs() -> None:
    first = outcome({"token": "a1", "quantity": 1})
    second = outcome({"token": "b2", "quantity": 1})

    comparison = compare_runs([first, second], [first, second], ("result",))

    assert comparison.status == "refuted"
    assert comparison.ignored_fields == 1


def test_compare_runs_matches_created_rows_by_creation_order_not_raw_ids() -> None:
    base = outcome(changes=created_orders({"id": 40, "product_id": 7, "quantity": 1, "created_at": "t1"}))
    head = outcome(changes=created_orders({"id": 41, "product_id": 7, "quantity": 1, "created_at": "t2"}))
    head["changes"][0]["created"][0]["id"] = 8
    head["changes"][1]["created"][0]["product_id"] = 8

    comparison = compare_runs([base, base], [head, head], ("Order",))

    assert comparison.status == "refuted"
    assert comparison.unpredicted == ()


def test_compare_runs_with_import_error_in_base_is_inconclusive() -> None:
    base = outcome(exception="ImportError: cannot import name 'new_entrypoint'")
    head = outcome({"ok": True})

    comparison = compare_runs([base, base], [head, head], ("result",))

    assert comparison.status == "inconclusive"
    assert "base에 시나리오 진입점이 없음" in comparison.summary


def test_validate_predictions_rejects_free_text() -> None:
    assert validate_predictions(["Product.stock", "shop.Order", "exception"]) == (
        "Product.stock", "shop.Order", "exception")
    with pytest.raises(ValueError, match="predicted_changes"):
        validate_predictions(["재고가 음수가 됨"])


def test_plan_requires_predicted_changes_for_differential_mode() -> None:
    raw = json.dumps({"version": 1, "head_sha": "a" * 40, "cases": [{
        "finding_index": 0, "mode": "differential", "condition": "c", "oracle": "o",
        "predicted_changes": [], "script": "def reproduce():\n    return {}\n",
    }]})

    with pytest.raises(ValueError, match="predicted_changes"):
        ReproductionPlan.from_json(raw, 1)


def test_plan_schema_requires_mode_and_predicted_changes() -> None:
    required = PLAN_SCHEMA["properties"]["cases"]["items"]["required"]

    assert {"mode", "predicted_changes"} <= set(required)


def test_plan_prompt_names_blocker_when_differential_is_unavailable() -> None:
    review = Review.from_json(json.dumps(impact_payload()))

    prompt = build_plan_prompt(review, "a" * 40, "마이그레이션 변경 PR")

    assert "Base/head differential execution is unavailable: 마이그레이션 변경 PR" in prompt
    assert "Prefer mode `differential`" in build_plan_prompt(review, "a" * 40, None)


def test_differential_blocker_rejects_migration_and_dependency_changes(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    assert differential_blocker(shop.path, shop.base_sha, shop.head_sha) is None

    (shop.path / "shop" / "migrations" / "0002_note.py").write_text("# migration\n", encoding="utf-8")
    _git(shop.path, "add", "-A")
    _git(shop.path, "commit", "-qm", "migration")
    migration_sha = _git(shop.path, "rev-parse", "HEAD")
    assert "마이그레이션 변경 PR" in differential_blocker(shop.path, shop.base_sha, migration_sha)

    (shop.path / "requirements-ci.txt").write_text("django\n", encoding="utf-8")
    _git(shop.path, "add", "-A")
    _git(shop.path, "commit", "-qm", "deps")
    assert "requirements-ci.txt" in differential_blocker(shop.path, migration_sha, _git(shop.path, "rev-parse", "HEAD"))


def _differential_case(quantity: int) -> ReproductionCase:
    return ReproductionCase(0, "재고보다 많은 수량을 주문", "재고는 음수가 되면 안 됨",
                            ORDER_SCRIPT.format(quantity=quantity), ("shop/services.py:12",),
                            "differential", ("Product.stock",))


def _run(shop, case):
    plan = ReproductionPlan(shop.head_sha, (case,))
    with base_worktree(shop.path, shop.base_sha, shop.head_sha) as base:
        return run_reproductions(plan, shop.path, sys.executable, 120, ("ERP_LIVE_SEND=false",), base)


def _stock(database) -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "select p.stock, (select count(*) from shop_order), (select count(*) from shop_orderlog) "
            "from shop_product p"
        ).fetchall()


def test_differential_run_confirms_negative_stock_when_head_drops_stock_check(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch, seed_stock=1)

    result = _run(shop, _differential_case(quantity=3)).results[0]

    assert result.status == "confirmed", result.observed
    assert result.mode == "differential" and result.cleanup_verified and result.target_reached
    assert result.predicted_differences == ("Product(pk=1).stock  base 변경 없음 · head 1 → -2",)
    assert result.other_differences == (
        "예외  base OutOfStock: widget · head 없음",
        '반환값  base 없음 · head {"quantity": 3}',
        'Order 생성  base 0건 · head 1건 (product_id=1, quantity=3, status="placed")',
        'OrderLog 생성  base 0건 · head 1건 (order_id=<new:Order#1>, message="ordered 3")',
    )
    assert _stock(shop.database) == [(1, 0, 0)]


def test_differential_run_refutes_when_base_and_head_write_identical_rows(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch, seed_stock=5)

    result = _run(shop, _differential_case(quantity=2)).results[0]

    assert result.status == "refuted", result.observed
    assert result.observed == "base와 head의 DB 변경·반환값·예외가 모두 같음"
    assert _stock(shop.database) == [(5, 0, 0)]


def test_differential_run_without_base_checkout_is_inconclusive(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    plan = ReproductionPlan(shop.head_sha, (_differential_case(quantity=3),))

    result = run_reproductions(plan, shop.path, sys.executable, 120, (), None,
                               "마이그레이션 변경 PR이라 같은 테스트 DB에서 base와 비교할 수 없음").results[0]

    assert result.status == "inconclusive"
    assert result.observed.startswith("base·head 차분 실행 불가: 마이그레이션 변경 PR")


def test_confirmed_differential_finding_renders_base_and_head_values(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch, seed_stock=1)
    payload = impact_payload()
    payload["findings"] = [{
        "category": "bug", "problem": "재고 검사 제거로 재고가 음수가 됨",
        "impact": "재고 수량이 실제와 달라짐", "evidence": ["shop/services.py:12"], "policy_quote": None,
    }]
    review = Review.from_json(json.dumps(payload))

    final = finalize_review(review, _run(shop, _differential_case(quantity=3)))
    body = render_markdown(final, 7, shop.head_sha, "재고 검사 정리")

    assert "base·head 차분 실행 (각 2회, 실행마다 달라지는 값 제외)" in body
    assert "      Product(pk=1).stock  base 변경 없음 · head 1 → -2" in body
    assert "    그 밖의 base 대비 차이" in body
    assert Review.from_json(final.to_json()).reproduced_findings[0].mode == "differential"


def test_base_worktree_uses_merge_base_when_base_branch_moved_after_pr_branched(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    _git(shop.path, "checkout", "-q", "-b", "moved-base", shop.base_sha)
    (shop.path / "shop" / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(shop.path, "add", "-A")
    _git(shop.path, "commit", "-qm", "unrelated base change")
    moved_base = _git(shop.path, "rev-parse", "HEAD")
    _git(shop.path, "checkout", "-q", shop.head_sha)

    with base_worktree(shop.path, moved_base, shop.head_sha) as base:
        assert _git(base, "rev-parse", "HEAD") == shop.base_sha
        assert not (base / "shop" / "unrelated.py").exists()
    assert differential_blocker(shop.path, moved_base, shop.head_sha) is None
