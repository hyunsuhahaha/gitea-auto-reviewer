from pathlib import Path


def test_workflow_supports_manual_review_of_existing_pr() -> None:
    workflow = Path(".gitea/workflows/ai-review.yml").read_text(encoding="utf-8")
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
    assert "Run pytest" in workflow
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
