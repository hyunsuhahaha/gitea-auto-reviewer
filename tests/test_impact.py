import json
import shutil
import sys
from dataclasses import asdict

import pytest

from gitea_auto_reviewer.cli import main
from gitea_auto_reviewer.evidence import collect_evidence
from gitea_auto_reviewer.gitnexus import GitNexusImpact, index_repository
from gitea_auto_reviewer.impact import (
    analyze_impact,
    changed_functions,
    changed_ranges,
    extract_django,
    load_paths,
    mark_static_misses,
    orm_references,
)
from gitea_auto_reviewer.review import ImpactPath, Review, render_markdown
from shop_fixture import make_shop_repository
from test_review import impact_payload

PLACE_ORDER = "shop/services.py:place_order"

FAKE_GITNEXUS = r'''import json, sys
callers = {"place_order": [{"name": "order_view", "filePath": "shop/views.py"}]}
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    else:
        arguments = message["params"]["arguments"]
        items = callers.get(arguments["target"], []) if arguments["direction"] == "upstream" else []
        body = {"target": {"name": arguments["target"]}, "byDepth": {"1": items} if items else {}}
        result = {"content": [{"type": "text", "text": json.dumps(body) + "\n\n---\n**Next:** hint"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''


def test_changed_functions_maps_diff_hunks_to_python_qualnames(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)

    ranges = changed_ranges(shop.path, shop.base_sha, shop.head_sha)

    assert list(ranges) == ["shop/services.py"]
    assert [item.symbol for item in changed_functions(shop.path, ranges)] == [PLACE_ORDER]


def test_django_extractor_reads_signal_receivers_routes_and_reverse_relations(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)

    django = extract_django(shop.path, sys.executable)

    assert {"signal": "post_save", "sender": "shop.Order",
            "receiver": {"file": "shop/signals.py", "line": 7, "name": "log_order"}} in django["signals"]
    assert django["urls"] == [{"route": "/products/<int:product_id>/order/",
                               "view": {"file": "shop/views.py", "line": 6, "name": "order_view"}}]
    product = next(item for item in django["models"] if item["label"] == "shop.Product")
    assert product["reverse"] == [{"accessor": "orders", "related_model": "shop.Order", "field": "product"}]


def test_orm_references_resolve_string_lookups_to_model_fields(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    models = extract_django(shop.path, sys.executable)["models"]

    references = {(item.model, item.field, item.access, item.function)
                  for item in orm_references(shop.path, models)}

    assert ("shop.Product", "stock", "read", "shop/reports.py:low_stock_products") in references
    assert ("shop.Order", "status", "read", "shop/reports.py:placed_quantities") in references
    assert ("shop.Product", "stock", "write", PLACE_ORDER) in references
    assert ("shop.Order", "quantity", "write", PLACE_ORDER) in references
    assert not any(item[3] and item[3].endswith("_spec.py") for item in references)


def test_pytest_trace_records_caller_chain_and_signal_callee_of_changed_function(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    trace = tmp_path / "trace.json"

    evidence = collect_evidence(shop.path, shop.head_sha, sys.executable, 300, "pytest", shop.base_sha, trace)

    assert evidence.pytest.status == "pass"
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["callers"][PLACE_ORDER] == [{"chain": [
        "shop/views.py:order_view", "shop/orders_spec.py:test_order_view_logs_order_and_decrements_stock",
    ], "count": 1}]
    assert payload["callees"][PLACE_ORDER] == [{"callee": "shop/signals.py:log_order", "line": 7}]
    assert payload["tests"][PLACE_ORDER] == ["shop/orders_spec.py::test_order_view_logs_order_and_decrements_stock"]


def test_trace_output_without_base_sha_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires the base SHA"):
        collect_evidence(tmp_path, "a" * 40, "python", 1, "pytest", None, tmp_path / "trace.json")


def _paths(document):
    return {(item["kind"], item["source"], item["target"]): item for item in document["paths"]}


def test_analyze_impact_finds_signal_and_orm_readers_of_changed_service(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)

    document = analyze_impact(shop.path, shop.base_sha, shop.head_sha, sys.executable)
    paths = _paths(document)

    assert document["changed_symbols"] == [PLACE_ORDER]
    assert document["sources"] == {"runtime": "not_collected", "django": "collected", "gitnexus": "not_requested"}
    signal = paths[("signal", PLACE_ORDER, "shop/signals.py:log_order")]
    assert (signal["detail"], signal["evidence"], signal["static_missed"]) == (
        "Order post_save", "shop/signals.py:7", None)
    reader = paths[("orm_field", PLACE_ORDER, "shop/reports.py:low_stock_products")]
    assert (reader["detail"], reader["evidence"]) == ("Product.stock 읽기", "shop/reports.py:9")
    assert ("orm_field", PLACE_ORDER, "shop/reports.py:placed_quantities") in paths


def test_runtime_edge_that_repeats_signal_edge_is_merged_as_confirmation(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    runtime = {"callers": {PLACE_ORDER: [{"chain": ["shop/views.py:order_view", "shop/orders_spec.py:test"],
                                          "count": 1}]},
               "callees": {PLACE_ORDER: [{"callee": "shop/signals.py:log_order", "line": 7}]}}

    paths = _paths(analyze_impact(shop.path, shop.base_sha, shop.head_sha, sys.executable, runtime))

    assert ("runtime_callee", PLACE_ORDER, "shop/signals.py:log_order") not in paths
    assert paths[("signal", PLACE_ORDER, "shop/signals.py:log_order")]["detail"] == \
        "Order post_save · pytest 실행으로 확인"
    caller = paths[("runtime_caller", "shop/views.py:order_view", PLACE_ORDER)]
    assert caller["detail"] == "실행 체인 진입점 shop/orders_spec.py:test"


def test_mark_static_misses_flags_only_paths_absent_from_gitnexus() -> None:
    signal = ImpactPath("signal", PLACE_ORDER, "shop/signals.py:log_order", "Order post_save",
                        "shop/signals.py:7", "django", "downstream")
    caller = ImpactPath("runtime_caller", "shop/views.py:order_view", PLACE_ORDER, "실행",
                        "shop/views.py:order_view", "runtime", "upstream")
    route = ImpactPath("url", "/products/<int:product_id>/order/", "shop/views.py:order_view", "URL 진입점",
                       "shop/views.py:6", "django", "upstream")
    static = {(PLACE_ORDER, "upstream"): {("shop/views.py", "order_view")}, (PLACE_ORDER, "downstream"): set()}

    marked = mark_static_misses([signal, caller, route], static)

    assert [item.static_missed for item in marked] == [True, False, None]


def test_gitnexus_impact_client_parses_mcp_impact_results(tmp_path) -> None:
    (tmp_path / "mcp").write_text(FAKE_GITNEXUS, encoding="utf-8")
    client = GitNexusImpact(sys.executable, tmp_path, timeout=30)
    try:
        assert client("shop/services.py", "place_order", "upstream") == {("shop/views.py", "order_view")}
        assert client("shop/services.py", "place_order", "downstream") == set()
    finally:
        client.close()


def test_impact_command_writes_document_and_review_renders_static_misses(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    (shop.path / "mcp").write_text(FAKE_GITNEXUS, encoding="utf-8")
    output = tmp_path / "impact.json"

    assert main(["impact", "--base-sha", shop.base_sha, "--head-sha", shop.head_sha,
                 "--repo-dir", str(shop.path), "--python", sys.executable,
                 "--gitnexus-binary", sys.executable, "--output", str(output)]) == 0

    paths = load_paths(output.read_text(encoding="utf-8"), shop.head_sha)
    assert {item.target: item.static_missed for item in paths} == {
        "shop/signals.py:log_order": True,
        "shop/reports.py:low_stock_products": True,
        "shop/reports.py:placed_quantities": True,
    }
    payload = impact_payload()
    payload["impact_paths"] = [asdict(item) for item in paths]
    body = render_markdown(Review.from_json(json.dumps(payload)), 3, shop.head_sha, "재고 검사 정리")
    assert "※ Django 구조·ORM 필드 참조·pytest 실행 추적으로 찾은 경로 · ⚠ GitNexus 정적 그래프에 없는 경로" in body
    assert "  • [signal] shop/services.py:place_order → shop/signals.py:log_order ⚠" in body
    assert "      Order post_save — shop/signals.py:7" in body


def test_impact_document_for_another_head_is_rejected() -> None:
    raw = json.dumps({"version": 1, "head_sha": "b" * 40, "paths": []})

    with pytest.raises(ValueError, match="different PR head SHA"):
        load_paths(raw, "a" * 40)


def test_review_without_impact_paths_field_still_loads() -> None:
    payload = impact_payload()
    payload.pop("impact_paths", None)

    assert Review.from_json(json.dumps(payload)).impact_paths == ()


@pytest.mark.skipif(shutil.which("gitnexus") is None, reason="GitNexus CLI is not installed")
def test_real_gitnexus_misses_signal_and_orm_paths_that_impact_analysis_finds(tmp_path, monkeypatch) -> None:
    shop = make_shop_repository(tmp_path, monkeypatch)
    index_repository(shop.path, shop.head_sha, "gitnexus")
    trace = tmp_path / "trace.json"
    collect_evidence(shop.path, shop.head_sha, sys.executable, 300, "pytest", shop.base_sha, trace)
    client = GitNexusImpact("gitnexus", shop.path)
    try:
        document = analyze_impact(shop.path, shop.base_sha, shop.head_sha, sys.executable,
                                  json.loads(trace.read_text(encoding="utf-8")), client)
    finally:
        client.close()

    paths = _paths(document)
    assert document["sources"]["gitnexus"] == "compared"
    assert paths[("runtime_caller", "shop/views.py:order_view", PLACE_ORDER)]["static_missed"] is False
    assert paths[("signal", PLACE_ORDER, "shop/signals.py:log_order")]["static_missed"] is True
    assert paths[("orm_field", PLACE_ORDER, "shop/reports.py:low_stock_products")]["static_missed"] is True
