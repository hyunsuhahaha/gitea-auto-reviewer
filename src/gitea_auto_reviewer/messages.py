"""User-visible text in every supported review language."""

from __future__ import annotations

LANGUAGE_NAMES = {"en": "English", "ko": "Korean"}

MESSAGES: dict[str, dict[str, str]] = {
    # PR comment summary
    "changed_files": {"en": "Changed files", "ko": "변경 파일"},
    "file_count": {"en": "{count}", "ko": "{count}개"},
    "database_change": {"en": "DB schema change", "ko": "DB 스키마 변경"},
    "data_change": {"en": "Data processing", "ko": "데이터 처리 변경"},
    "api_contract": {"en": "API contract", "ko": "API Contract"},
    "external_integration": {"en": "Integrations", "ko": "외부연동"},
    "impact_yes": {"en": "yes", "ko": "있음"},
    "impact_changed": {"en": "changed", "ko": "변경"},
    "impact_affected": {"en": "affected", "ko": "영향 있음"},
    "impact_possible": {"en": "possible", "ko": "영향 가능"},
    "impact_not_detected": {"en": "no direct impact found", "ko": "직접 영향 미발견"},
    "verified_facts": {"en": "Verified facts", "ko": "검증된 사실"},
    "tests": {"en": "Tests (pytest)", "ko": "테스트(pytest)"},
    "migration_no_missing": {"en": "no missing migrations", "ko": "누락 없음"},
    "migration_missing": {"en": "missing migrations", "ko": "누락 감지"},
    "risk": {"en": "Risk", "ko": "위험도"},
    "risk_basis": {"en": "evidence", "ko": "근거"},
    "key_changes": {"en": "Key changes", "ko": "주요 변경"},
    "more_changes": {"en": "{count} more related changes", "ko": "그 외 관련 변경 {count}건"},
    # findings
    "reproduced": {"en": "Reproduced problems", "ko": "재현된 문제"},
    "no_reproduced": {"en": "No confirmed reproduced problem", "ko": "확정된 재현 문제 없음"},
    "unreproduced": {"en": "Findings not reproduced", "ko": "재현하지 못한 발견 사항"},
    "unreproduced_note": {
        "en": "※ Not run, failed, timed out, did not reach the changed code, or showed no problem",
        "ko": "※ 미실행, 실행 오류·시간초과·코드 미도달 또는 실행상 현상 미관찰",
    },
    "rejected": {"en": "Reproduced but rejected on second review", "ko": "재현 성공 후 2차 검증 미채택"},
    "rejected_note": {
        "en": "※ DB reproduction, code reach, and rollback succeeded, but the second review rejected it",
        "ko": "※ DB 재현·코드 도달·롤백 검증은 성공했으나 2차 검증에서 미채택",
    },
    "impact": {"en": "Impact", "ko": "영향"},
    "rejection_reason": {"en": "Rejected because", "ko": "미채택 사유"},
    "reproduction_result": {"en": "Reproduction", "ko": "재현 결과"},
    "status_unplanned": {"en": "not run", "ko": "미실행"},
    "status_inconclusive": {"en": "inconclusive", "ko": "실행 불확정"},
    "status_not_reproduced": {"en": "not reproduced", "ko": "실행상 미재현"},
    "policy_rule": {"en": "Rule", "ko": "규칙"},
    "new_marker": {"en": "[new] ", "ko": "[신규] "},
    "conditions": {"en": "Conditions used", "ko": "재현에 사용한 조건"},
    "condition_rate": {"en": "Bug condition rate", "ko": "버그 조건 충족률"},
    "rows": {"en": " rows", "ko": "건"},
    "differential_runs": {
        "en": "Base vs head differential run (2 runs each, run-specific values excluded)",
        "ko": "base·head 차분 실행 (각 2회, 실행마다 달라지는 값 제외)",
    },
    "other_differences": {"en": "Other differences from base", "ko": "그 밖의 base 대비 차이"},
    "observed": {"en": "Observed", "ko": "관찰 결과"},
    "reached": {"en": "Executed", "ko": "실행 도달"},
    "rollback_passed": {"en": "Rollback check: passed", "ko": "롤백 검증: 통과"},
    "previous_commit": {"en": "Reproduced in the previous commit ({sha})",
                        "ko": "이전 커밋({sha})에서 재현됐던 문제"},
    "previous_commit_note": {
        "en": "※ Not confirmed again in this commit — it may be fixed, or the reproduction path changed",
        "ko": "※ 이번 커밋의 재현 결과에서 같은 문제가 확인되지 않음 — 수정됐거나 재현 경로가 달라졌을 수 있음",
    },
    "impact_paths": {"en": "Impact paths", "ko": "영향 경로"},
    "impact_paths_note": {"en": "※ Found from Django structure, ORM field references, and pytest call traces",
                          "ko": "※ Django 구조·ORM 필드 참조·pytest 실행 추적으로 찾은 경로"},
    "impact_paths_missed": {"en": " · ⚠ absent from the GitNexus static graph",
                            "ko": " · ⚠ GitNexus 정적 그래프에 없는 경로"},
    "path_signal": {"en": "signal", "ko": "signal"},
    "path_orm_field": {"en": "ORM field", "ko": "ORM 필드"},
    "path_url": {"en": "URL", "ko": "URL"},
    "path_runtime_caller": {"en": "runtime caller", "ko": "실행 호출자"},
    "path_runtime_callee": {"en": "runtime callee", "ko": "실행 피호출자"},
    "more_paths": {"en": "{count} more impact paths", "ko": "그 외 영향 경로 {count}건"},
    "affected_files": {"en": "Affected files", "ko": "영향 파일"},
    # impact path details
    "detail_url": {"en": "URL entry point", "ko": "URL 진입점"},
    "detail_chain_entry": {"en": "call chain entry {entry}", "ko": "실행 체인 진입점 {entry}"},
    "detail_runtime_call": {"en": "called while pytest ran", "ko": "pytest 실행 중 호출"},
    "detail_field_change": {"en": "{field} definition changed · {access}", "ko": "{field} 정의 변경 · {access}"},
    "detail_confirmed": {"en": "confirmed by pytest", "ko": "pytest 실행으로 확인"},
    "access_write": {"en": "write", "ko": "쓰기"},
    "access_read": {"en": "read", "ko": "읽기"},
    # reproduction outcomes
    "no_base_sha": {"en": "base SHA was not provided", "ko": "base SHA가 제공되지 않음"},
    "no_base_checkout": {"en": "base checkout was not provided", "ko": "base 체크아웃이 제공되지 않음"},
    "blocker_migration": {"en": "migrations changed, so base cannot share the test DB: {path}",
                          "ko": "마이그레이션 변경 PR이라 같은 테스트 DB에서 base와 비교할 수 없음: {path}"},
    "blocker_dependency": {"en": "dependencies changed, so base cannot share the venv: {path}",
                           "ko": "의존성 파일 변경 PR이라 같은 venv에서 base를 실행할 수 없음: {path}"},
    "same_as_base": {"en": "same as the base run", "ko": "base 실행 결과와 동일"},
    "differential_unavailable": {"en": "base/head differential run unavailable: {reason}",
                                 "ko": "base·head 차분 실행 불가: {reason}"},
    "head_failed": {"en": "head run failed: {detail}", "ko": "head 실행 실패: {detail}"},
    "base_failed": {"en": "base run failed: {detail}", "ko": "base 실행 실패: {detail}"},
    "rollback_unverified": {"en": "could not verify the DB was restored after rollback",
                            "ko": "롤백 후 DB 복원을 확인하지 못함"},
    "repair_failed": {"en": "automatic repair failed: {error}", "ko": "자동 수정 실패: {error}"},
    "repair_unplanned": {"en": "no automatic repair plan", "ko": "자동 수정 계획 없음"},
    "repair_still_failing": {"en": "still failing after one automatic repair: {detail}",
                             "ko": "자동 수정 1회 후에도 실패: {detail}"},
    "detail_unplanned": {"en": "not planned within the Django/ORM rollback reproduction scope",
                         "ko": "현재 Django/ORM 롤백 재현 범위에서 계획되지 않음"},
    "detail_target_unreached": {"en": "execution of the changed code was not confirmed",
                                "ko": "변경 근거 코드 도달이 확인되지 않음"},
    "detail_no_rejection_reason": {"en": "the second review recorded no rejection reason",
                                   "ko": "2차 검증 미채택 사유가 기록되지 않음"},
    "detail_not_observed": {"en": "the problem was not observed when the conditions ran",
                            "ko": "조건 실행에서 문제를 관찰하지 못함"},
    "detail_undecided": {"en": "the run could not be judged", "ko": "실행 결과를 판정하지 못함"},
    "runner_target_unreached": {"en": "did not reach the changed code: ", "ko": "변경 근거 코드에 도달하지 못함: "},
    "runner_no_targets": {"en": "no executable Python target evidence",
                          "ko": "실행 가능한 Python target evidence가 없음"},
    "runner_no_values": {"en": "no expected/observed values", "ko": "expected/observed 관찰값이 없음"},
    # differential comparison
    "fact_exception": {"en": "exception", "ko": "예외"},
    "fact_result": {"en": "return value", "ko": "반환값"},
    "fact_none": {"en": "none", "ko": "없음"},
    "fact_created": {"en": "{model} created", "ko": "{model} 생성"},
    "fact_count": {"en": "{count} rows", "ko": "{count}건"},
    "fact_new_row": {"en": "{model}[new#{index}].{field}", "ko": "{model}[신규#{index}].{field}"},
    "fact_deleted_row": {"en": "{model}(pk={pk}) deleted", "ko": "{model}(pk={pk}) 삭제"},
    "fact_deleted": {"en": "deleted", "ko": "삭제됨"},
    "fact_unchanged": {"en": "unchanged", "ko": "변경 없음"},
    "missing_entrypoint": {"en": "the scenario entry point does not exist on base: {error}",
                           "ko": "base에 시나리오 진입점이 없음: {error}"},
    "runs_identical": {"en": "base and head DB changes, return values, and exceptions are identical",
                       "ko": "base와 head의 DB 변경·반환값·예외가 모두 같음"},
    "predicted_difference": {"en": "predicted difference from base observed: ", "ko": "base 대비 예측한 차이 관찰: "},
    "unpredicted_only": {"en": "no difference where predicted; only other differences: ",
                         "ko": "예측한 위치의 차이는 없고 다른 차이만 관찰: "},
}


def text(language: str, key: str, **values: object) -> str:
    if language not in LANGUAGE_NAMES:
        raise ValueError(f"unsupported language: {language}")
    return MESSAGES[key][language].format(**values)


def language_name(language: str) -> str:
    if language not in LANGUAGE_NAMES:
        raise ValueError(f"unsupported language: {language}")
    return LANGUAGE_NAMES[language]
