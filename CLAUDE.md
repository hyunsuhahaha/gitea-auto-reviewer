# Project Instructions

## Purpose and Stack

- Python 3.11+ CLI package using a `src/` layout and only the standard library at runtime.
- Bridges trusted same-repository Gitea PR workflows to the Codex CLI.
- Uses setuptools for packaging and pytest 8+ for tests.

## Security Architecture

- Preserve the three-stage credential boundary: `evidence` may execute PR code, `review` may access Codex credentials but no Gitea token, and `comment` may access the Gitea token but never invokes Codex or PR code.
- Treat PR-head files and diff content as untrusted. Read review policy from the base commit (`base:AI_REVIEW.md`).
- Bind evidence and review inputs to the exact full head SHA.
- Keep Codex execution read-only, ephemeral, isolated from user rules/config, and supplied with an allowlisted environment.
- Do not broaden v0.1 beyond trusted internal, same-repository PRs without an explicit threat-model change.

## Code Style

- Follow the existing small-module, standard-library-first design; avoid new dependencies unless clearly necessary.
- Use `snake_case` modules/functions, `PascalCase` classes, type hints, and frozen dataclasses for validated value objects.
- Validate external/model JSON strictly: exact fields, bounded lengths/counts, enums, safe paths, and concrete file-line evidence.
- Raise `ValueError` for invalid caller input, `RuntimeError` for local process failures, and `GiteaAPIError` for API failures.
- Keep subprocess argument lists explicit and child environments allowlisted.

## Build and Test

- Install development dependencies: `python -m pip install -e ".[dev]"`
- Run tests: `python -m pytest`
- Run a focused test: `python -m pytest tests/test_review.py`
- Build/install: `python -m pip install .`
- CLI help: `python -m gitea_auto_reviewer --help`
- Test files are named `tests/test_<module>.py`; use pytest functions, `monkeypatch`, and `tmp_path`.

## Project Structure

- `src/gitea_auto_reviewer/cli.py`: `evidence`, `review`, and `comment` orchestration.
- `src/gitea_auto_reviewer/evidence.py`: isolated Django/migration/pytest checks and SHA-bound evidence JSON.
- `src/gitea_auto_reviewer/git_context.py`: trusted-base policy and base-to-head diff collection; review prompts.
- `src/gitea_auto_reviewer/codex.py`: credential-filtered, read-only Codex CLI wrapper.
- `src/gitea_auto_reviewer/review.py`: review schema, validation, grounding, verification, and Markdown rendering.
- `src/gitea_auto_reviewer/gitea.py`: minimal Gitea issue-comment client with marker-based upsert.
- `.gitea/workflows/ai-review.yml`: Windows Gitea Actions reference workflow.
- `AI_REVIEW.md`: project-specific review policy consumed from the trusted base commit.

## Change Conventions

- Use Conventional Commit subjects (`feat:`, `fix:`, `perf:`), matching repository history.
- Add or update focused tests for every boundary, validation, subprocess, or API behavior change.
- Preserve ordinary-comment-only behavior: never add approval, rejection, merge, push, or branch-protection actions.
- Keep the workflow compatible with PowerShell and the dedicated Windows runner paths unless scope explicitly changes.
