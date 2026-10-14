import pytest
from pathlib import Path


def test_workflow_supports_manual_review_of_existing_pr() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
    assert 'run-name: "AI Review PR #${{ gitea.event.inputs.pr_number || gitea.event.pull_request.number }}"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "pr_number:" in workflow
    assert "Resolve PR metadata" in workflow
    assert "pull-requests: read" in workflow
    assert "METADATA_GITEA_TOKEN: ${{ secrets.GITEA_TOKEN }}" in workflow
    assert '--token-env "METADATA_GITEA_TOKEN"' in workflow
    assert "steps.metadata.outputs.base_sha" in workflow
    assert "steps.metadata.outputs.head_sha" in workflow
    assert "steps.metadata.outputs.pr_number" in workflow
    assert "Run Django system check" in workflow
    assert "Run migration check" in workflow
    assert "Run pytest with call tracing" in workflow
    assert "Combine deterministic evidence" in workflow
    assert "Show Codex reasoning settings" in workflow
    assert "vars.AI_REVIEW_FIRST_PASS_EFFORT" in workflow
    assert "vars.AI_REVIEW_PLAN_EFFORT" in workflow
    assert "vars.AI_REVIEW_VERIFY_EFFORT" in workflow
    first = workflow.index("Generate first-pass Codex findings")
    reproduce = workflow.index("Reproduce candidate findings with rollback")
    verify = workflow.index("Verify reproduced findings with Codex")
    publish = workflow.index("Keep only reproduced findings")
    assert first < reproduce < verify < publish
    assert 'gitea-auto-review*.json' in workflow
    assert 'debug-runs' in workflow
    assert '$env:GITHUB_RUN_ID' in workflow


def test_workflow_cancels_superseded_reviews_and_bounds_runtime() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
    assert "concurrency:" in workflow
    assert "group: ai-review-${{ gitea.event.inputs.pr_number || gitea.event.pull_request.number }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes:" in workflow


def test_workflow_gates_every_review_step_on_skip() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
    steps = workflow.split("      - name: ")[1:]
    gated = [step for step in steps if "steps.metadata.outputs.skip != 'true'" in step]
    names = [step.splitlines()[0] for step in steps]
    assert len(gated) == len(steps) - 2
    assert names[0] == "Resolve PR metadata"
    assert names[-1] == "Preserve last-run debug artifacts"
    assert 'if (-not $env:MANUAL_PR_NUMBER) { $skipArgs += "--allow-skip" }' in workflow


def test_workflow_feeds_traced_impact_paths_and_base_sha_into_review_stages() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
    pytest_step = workflow.index("Run pytest with call tracing")
    impact = workflow.index("Find Django impact paths missing from the static graph")
    first = workflow.index("Generate first-pass Codex findings")
    assert pytest_step < impact < first
    assert '--trace-output "$env:TRACE_FILE"' in workflow
    assert '--runtime-trace "$env:TRACE_FILE"' in workflow
    assert '--impact-file "$env:IMPACT_FILE"' in workflow
    plan = workflow[workflow.index("Plan rollback-only reproductions"):workflow.index("Reproduce candidate")]
    reproduce = workflow[workflow.index("Reproduce candidate"):workflow.index("Verify reproduced")]
    assert '--base-sha "$env:BASE_SHA"' in plan
    assert '--base-sha "$env:BASE_SHA"' in reproduce


def test_workflow_resolves_base_config_once_for_all_steps() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
    metadata = workflow[workflow.index("Resolve PR metadata"):workflow.index("Check out exact PR head")]
    assert '--config-output "$env:CONFIG_FILE"' in metadata
    assert "AppendAllText($env:GITHUB_ENV" in metadata
    # Live-integration guards stay in the workflow even when a repository has no config yet.
    assert '--require-setting "ERP_LIVE_SEND=false"' in workflow


WORKFLOWS = (Path(".gitea/workflows/ai-review.yml"), Path("examples/workflows/ai-review-linux.yml"))


def _steps(path: Path) -> dict[str, str]:
    steps = path.read_text(encoding="utf-8").split("      - name: ")[1:]
    return {step.splitlines()[0]: step for step in steps}


def test_linux_and_windows_workflows_run_the_same_steps_in_order() -> None:
    windows, linux = (list(_steps(path)) for path in WORKFLOWS)
    assert linux == windows


@pytest.mark.parametrize("path", WORKFLOWS, ids=["windows", "linux"])
def test_workflow_keeps_credential_stages_separate(path) -> None:
    steps = _steps(path)
    for name, step in steps.items():
        if name == "Post or update PR comment":
            assert "secrets.AI_REVIEW_GITEA_TOKEN" in step
            continue
        assert "AI_REVIEW_GITEA_TOKEN" not in step, name
        if name in {"Generate first-pass Codex findings", "Plan rollback-only reproductions",
                    "Verify reproduced findings with Codex",
                    "Find Django impact paths missing from the static graph"}:
            assert 'GITEA_TOKEN: ""' in step, name
    gated = [name for name, step in steps.items() if "steps.metadata.outputs.skip != 'true'" in step]
    assert gated == list(steps)[1:-1]
    assert "--config-output" in steps["Resolve PR metadata"]
    assert "AI_REVIEW_CONFIG_FILE=" in steps["Resolve PR metadata"]


def test_linux_workflow_is_portable_between_gitea_and_forgejo() -> None:
    workflow = WORKFLOWS[1].read_text(encoding="utf-8")
    assert "gitea." not in workflow.replace("gitea-auto-reviewer", "").replace("gitea-auto-review", "")
    assert "C:\\" not in workflow and "powershell" not in workflow
    assert "ai-review-codex:/codex-home" in workflow


@pytest.mark.parametrize("path", WORKFLOWS, ids=["windows", "linux"])
def test_review_workflow_selects_tests_before_any_pr_code_runs(path) -> None:
    steps = _steps(path)
    names = list(steps)
    assert (names.index("Fetch base commit as data") < names.index("Restore test impact map")
            < names.index("Select tests affected by the change") < names.index("Create isolated CI environment"))
    assert "actions/cache/restore@v4" in steps["Restore test impact map"]
    # Review runs execute PR code, so they may read the map but never write the cache.
    workflow = path.read_text(encoding="utf-8")
    assert "actions/cache/save" not in workflow and "actions/cache@" not in workflow
    assert "test-select" in steps["Select tests affected by the change"]
    assert "--selection-file" in steps["Run pytest with call tracing"]


MAP_WORKFLOWS = (Path(".gitea/workflows/ai-review-test-map.yml"),
                 Path("examples/workflows/ai-review-test-map-linux.yml"))


def test_linux_and_windows_map_workflows_run_the_same_steps_in_order() -> None:
    windows, linux = (list(_steps(path)) for path in MAP_WORKFLOWS)
    assert linux == windows


@pytest.mark.parametrize("path", MAP_WORKFLOWS, ids=["windows", "linux"])
def test_map_workflow_records_only_base_branch_pushes_into_the_restored_cache(path) -> None:
    workflow = path.read_text(encoding="utf-8")
    steps = _steps(path)
    assert "pull_request" not in workflow and "secrets." not in workflow
    assert "--record-test-map" in steps["Record test impact map"]
    save = steps["Save test impact map"]
    assert "actions/cache/save@v4" in save
    review = _steps(WORKFLOWS[MAP_WORKFLOWS.index(path)])["Restore test impact map"]
    for line in ("path: .ai-review-test-map", "key: ai-review-test-map-"):
        assert line in save and line in review
