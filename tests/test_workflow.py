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
