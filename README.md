# gitea-auto-reviewer

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![stdlib only](https://img.shields.io/badge/runtime%20deps-stdlib%20only-informational.svg)](pyproject.toml)

**The self-hosted Gitea reviewer that doesn't just trust the LLM.** Most AI PR
reviewers — including the other self-hosted Gitea bots — read a diff, ask an
LLM for an opinion, and post it. This one goes further: every finding is
re-run against a real test database inside a forced-rollback transaction
before it's allowed anywhere near a PR comment. No reproduction, no comment.

`gitea-auto-reviewer` is a compact AI change-impact summary for self-hosted
Gitea. It connects an existing Codex subscription to pull-request comments and
explains what changed, what new assumptions the change introduces, where it may
have an operational impact, and what a person should verify. It does not require
an OpenAI API key or a Copilot seat.

It is an AI-assisted first-pass reviewer, not a merge gate. It can create or
update one ordinary PR comment. It cannot approve, request changes, merge,
push commits, or modify branch protection.

## Review and reproduction flow

```text
PR head ── existing Windows test runner
              ├─ GitNexus analyze at the exact head SHA
              ├─ Django check
              ├─ migration check
              ├─ pytest
              └─ evidence.json bound to head SHA
                          │
pull_request_target on trusted base workflow
              │
              ├─ verify same-repository PR
              ├─ check out PR head for read-only inspection
              ├─ fetch base object (do not execute PR code)
              ├─ git diff base...head
              └─ read base:AI_REVIEW.md
                          │
                    review process
                    Codex auth: yes
                    Gitea token: no
                    sandbox: read-only
                    GitNexus MCP: yes
                          │
                 candidate-review.json
                          │
                 Codex reproduction plan
                          │
               direct Django view/ORM calls
               test DB + transaction.atomic()
               forced rollback + cleanup check
                          │
                Codex rejection pass (low)
                reads reproduction evidence
                          │
              confirmed findings only
                     review.json
                          │
                    comment process
                    Codex auth: unused
                    Gitea token: yes
                          │
                 create/update PR comment
```

The CLI stages run sequentially in one Windows job:

```bash
gitea-auto-reviewer index ...    # build/update the PR-head code graph
gitea-auto-reviewer evidence ... # trusted internal PR execution, sanitized child environment
gitea-auto-reviewer review ...   # Codex session, consumes evidence, no Gitea credential
gitea-auto-reviewer plan ...     # Codex writes up to 3 objective reproduction cases
gitea-auto-reviewer reproduce ...# CI Python executes cases and forces DB rollback
gitea-auto-reviewer verify ...   # low-effort Codex pass challenges reproduced findings
gitea-auto-reviewer finalize ... # retain confirmed + cleanup-verified findings only
gitea-auto-reviewer comment ...  # Gitea credential, never invokes Codex or PR code
```

The Codex subprocess receives an allowlisted environment rather than the
runner's complete environment. It runs at the PR head repository with
`--sandbox read-only`, `--ephemeral`, `--ignore-user-config`, and
`--ignore-rules`. Codex can search and read the complete repository to trace a
change into callers, serializers, integrations, deployment files, and tests.
The prompt treats every repository file, including `AGENTS.md`, as untrusted
data and forbids running project code. The diff and trusted base-branch policy
tell Codex where to start and what operational risks matter.

`read-only` prevents repository writes but is not an operating-system-level
guarantee that no process can be started. The wrapper removes unrelated runner
secrets and forbids tests, builds, hooks, scripts, package managers, and network
access in the task. Deployments that require a hard no-exec boundary should run
the review stage in a separately hardened sandbox while mounting the repository
read-only. Deterministic execution remains the responsibility of CI.

## Scope and trust model

v0.2 intentionally supports only:

- trusted, persistent, self-hosted runners;
- private/internal repositories;
- PRs whose head and base belong to the same repository;
- a preinstalled, audited version of this package;
- a normal Markdown timeline comment;
- a maximum diff size of 1 MB by default.

Only the evidence process executes PR code. Codex is never asked to run tests,
builds, migrations, package managers, or project scripts. The Windows Server
trial reuses the existing test deployment runner. Check subprocesses receive an
allowlisted environment and a temporary HOME, so step-scoped Gitea credentials,
`CODEX_HOME`, and unrelated runner secrets are not inherited. This is process
separation, not account or VM isolation, so it is restricted to trusted internal
same-repository PRs. A finding never changes the workflow result solely because
of its severity.

Do not use this version for fork PRs or untrusted public repositories. A
self-hosted runner is privileged infrastructure; Gitea itself recommends that
runners and repositories trust each other.

## Requirements

- Python 3.11 or newer
- Git
- Codex CLI with `--output-schema` support
- GitNexus CLI
- A dedicated Windows Gitea runner with the `ai-review-windows` label
- A ChatGPT account entitled to use Codex
- A Gitea token that can read and write PR/issue comments

## Install on the runner

Install one audited release under the existing runner service account.
Do this during runner provisioning, not from a pull request:

```bash
python -m pip install "gitea-auto-reviewer==0.2.0"
```

For development before a package is published:

```bash
git clone https://github.com/hyunsuhahaha/gitea-auto-reviewer.git
cd gitea-auto-reviewer
python -m pip install .
python -m pip install uv
```

Install GitNexus once as the dedicated runner account (not inside each PR):

```powershell
npm install -g gitnexus
gitnexus --version
```

Do not run `pip install .` against an untrusted PR checkout. Python build hooks
are executable code.

## Authenticate Codex without an API key

Sign in once as the OS account behind the existing Windows runner:

```bash
codex login
codex login status
```

Choose **Sign in with ChatGPT**. The CLI caches the session and uses the Codex
entitlement attached to that ChatGPT account. No `OPENAI_API_KEY` is read or
required by this program.

Protect the runner account and its Codex credential store. A cached login can
expire or be revoked; in that case the review command fails with an instruction
to run `codex login` again. See the official
[Codex authentication documentation](https://learn.chatgpt.com/docs/auth).

## Configure Gitea

1. Create a dedicated Gitea bot or service account.
2. Give its token only the repository permissions needed to list, create, and
   edit issue/PR comments. Do not grant merge or administration permissions.
3. Store the token as the repository or organization Actions secret
   `AI_REVIEW_GITEA_TOKEN`.
4. Copy [`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml) into
   the reviewed repository's trusted base branch.
5. Install `uv` in the persistent reviewer virtual environment. Every PR still
   gets a fresh `.venv-ci`; uv reuses its Windows package cache to avoid slow
   repeated pip installations.
6. Ensure `gitea-auto-reviewer`, `codex`, and `gitnexus` are available to the dedicated
   runner account and that the runner has the `ai-review-windows` label.

The example uses `pull_request_target` so the workflow definition comes from
   the trusted base branch. It checks that the PR comes from the same repository,
   checks out the PR head, runs deterministic checks in a fresh CI environment,
   and keeps the later Codex process read-only and credential-free.

## CLI

Index the checked-out PR head before review. The command rejects a checkout
whose actual `HEAD` differs from the supplied SHA:

```powershell
gitea-auto-reviewer index `
  --head-sha 2222222222222222222222222222222222222222 `
  --repo-dir C:\runner\work\payments `
  --gitnexus-binary C:\Users\GiteaAIReview\AppData\Roaming\npm\gitnexus.cmd
```

Collect deterministic evidence on the credential-free Windows evidence runner:

```bash
gitea-auto-reviewer evidence `
  --head-sha 2222222222222222222222222222222222222222 `
  --repo-dir C:\runner\work\payments `
  --output C:\runner\temp\evidence.json
```

This runs `python manage.py check`, `python manage.py makemigrations --check
--dry-run`, and `python -m pytest -q -p no:cacheprovider`. It verifies that the checkout's actual
`HEAD` equals `--head-sha` before executing anything and records that SHA in the
evidence document.

The example workflow runs those three checks as separate Actions steps and
combines their SHA-bound JSON files with `evidence-merge`. This makes Django,
migration, and pytest duration visible independently without changing the
final evidence consumed by Codex.

The first Codex pass uses high reasoning effort for repository and boundary
analysis. After candidate findings are executed, the independent rejection
pass uses low effort to disprove or retain only those reproduced findings.

Generate a structured review:

```bash
gitea-auto-reviewer review \
  --repository acme/payments \
  --head-repository acme/payments \
  --pr 42 \
  --pr-title "상품 비고 기능 추가" \
  --base-sha 1111111111111111111111111111111111111111 \
  --head-sha 2222222222222222222222222222222222222222 \
  --repo-dir /runner/work/payments \
  --evidence-file /runner/temp/evidence.json \
  --gitnexus-binary gitnexus \
  --output /runner/temp/review.json
```

Both Codex passes receive the same repository-scoped GitNexus STDIO MCP
server. The first pass must query changed symbols, context, impact, and
affected processes before drafting. The low-effort rejection pass can query
the graph again to confirm or disprove findings. GitNexus supplies static
change-impact evidence; the reviewer verifies publishable claims against real
repository files and deterministic CI evidence.

Generate and execute rollback-only reproductions before publishing:

```powershell
gitea-auto-reviewer plan `
  --head-sha 2222222222222222222222222222222222222222 `
  --review-file C:\runner\temp\candidate-review.json `
  --output C:\runner\temp\reproduction-plan.json

gitea-auto-reviewer reproduce `
  --head-sha 2222222222222222222222222222222222222222 `
  --plan-file C:\runner\temp\reproduction-plan.json `
  --python .\.venv-ci\Scripts\python.exe `
  --require-setting ERP_LIVE_SEND=false `
  --require-setting MAIN_APP_RUN=false `
  --output C:\runner\temp\reproduction-evidence.json

gitea-auto-reviewer verify `
  --head-sha 2222222222222222222222222222222222222222 `
  --review-file C:\runner\temp\candidate-review.json `
  --reproduction-file C:\runner\temp\reproduction-evidence.json `
  --output C:\runner\temp\verification.json

gitea-auto-reviewer finalize `
  --head-sha 2222222222222222222222222222222222222222 `
  --review-file C:\runner\temp\candidate-review.json `
  --reproduction-file C:\runner\temp\reproduction-evidence.json `
  --verification-file C:\runner\temp\verification.json `
  --output C:\runner\temp\review.json
```

The plan contains Python harnesses, but transaction and cleanup control are not
delegated to the model. The fixed executor initializes Django, checks required
runtime settings, opens `transaction.atomic()` for every configured Django DB,
forces rollback, closes all DB
connections, and verifies declared rows/fields again on a fresh connection.
Only `confirmed` results whose cleanup checks pass are rendered under
`재현된 문제`. Refuted, timed-out, exceptional, and cleanup-unverified cases are
kept out of the PR comment. PostgreSQL sequence values can still have gaps;
sequences are not transactional.

Post or update the bot comment:

```bash
export GITEA_REVIEW_TOKEN='stored-by-the-runner-secret-manager'
gitea-auto-reviewer comment \
  --gitea-url https://gitea.example.com \
  --repository acme/payments \
  --pr 42 \
  --pr-title "상품 비고 기능 추가" \
  --head-sha 2222222222222222222222222222222222222222 \
  --review-file /runner/temp/review.json
```

The same values can be supplied through `GITEA_REPOSITORY`,
`GITEA_HEAD_REPOSITORY`, `GITEA_PR_NUMBER`, `GITEA_PR_TITLE`, `GITEA_BASE_SHA`,
`GITEA_HEAD_SHA`, `GITEA_URL`, and `GITEA_REVIEW_TOKEN`. Tokens are deliberately
environment-only so they do not appear in process listings or shell history.

### Manually review an existing or merged PR

The example workflow also supports `workflow_dispatch`. In the repository's
Actions page, choose **Codex AI review**, select **Run workflow**, enter only
the existing PR number, and run it. The metadata stage reads the original
title, base SHA, head SHA, and head repository from Gitea, then runs the same
index, CI, Codex, rollback-reproduction, finalize, and comment stages. This can
update the review comment on a merged PR without reopening it or creating a
test commit. The original head commit must still be available in the Gitea
repository.

## Change-impact format

Codex starts from the diff, then searches the complete PR head repository to
compare the meaning before and after the change and find concrete downstream
callers. It extracts the new assumptions introduced for callers, serializers,
database schema, SCM/ERP integrations, deployment, and rollback. The PR comment
shows only the compact result:

```text
┌────────────────────────────┐
│ PR #214 상품 비고 기능 추가 │
├────────────────────────────┤

변경 파일        7개
  └ product/models.py
  └ product/services.py
  └ product/api.py
  └ product/serializers.py
  └ scm/product_sync.py
  └ tests/test_product.py
  └ product/migrations/0008_product_remark.py
DB 스키마 변경   있음
  └ product_product 테이블에 remark 컬럼 추가 — product/migrations/0008_product_remark.py:18
데이터 처리 변경 있음
  └ Product.remark 저장값을 상품 조회 응답에 포함 — product/services.py:18
API Contract     변경
외부연동         영향 가능
  └ SCM 상품 동기화 경로에서 Product 생성 확인 — scm/product_sync.py:74

검증된 사실
✅ Django check PASS
✅ Migration check 누락 없음
✅ 테스트(pytest) 147/147 PASS

위험도           🟠 HIGH · 근거 HIGH
                  product/models.py:31

주요 변경
• Product.remark 추가
• 상품 등록·조회 API 변경

주의
• 기존 Product 생성 경로가 remark를 전달하지 않음
  영향: 상품 생성이 실패할 수 있음
  └ product/models.py:31
  └ product/services.py:18

영향 파일
• app/views/product_admin.py
  └ Product 생성 호출자가 새 필드의 영향을 받음 — app/views/product_admin.py:52
• scm/product_sync_service.py
  └ SCM 동기화 과정에서 변경된 Product 생성 경로를 호출함 — scm/product_sync_service.py:74
```

The program validates the structured result before posting it. The changed-file
count and paths come from Git. Django, migration, and pytest results come from the
evidence document and overwrite model output. Evidence whose `head_sha` does
not exactly match the reviewed PR head is rejected. A hidden marker lets later
runs update the existing comment without a database:

```html
<!-- gitea-auto-reviewer:pr=42:sha=abc123... -->
```

To reduce confident but low-value commentary, findings disappear when none
qualify. Only concrete bugs, security issues, performance issues, dependency
problems, and explicit base-policy violations are allowed. Each finding contains
only the demonstrated problem, its operational impact, and real `file:line` locations.
Policy findings must quote an exact rule from the base `AI_REVIEW.md`. The
program verifies files, line ranges, and policy quotations before publication.
Style, naming, and generic improvement opinions are forbidden. Risk and
confidence remain separate. Codex runs with high reasoning effort and must
evaluate changed conditions at equality/null/minimum/maximum boundaries, then
trace newly admitted states through callers before assigning risk.
Deterministic CI facts take precedence. A
second independent read-only Codex pass tries to disprove every draft finding;
it may only retain a finding verbatim or delete it, never add or rewrite one.
The final `영향 파일` section lists at most five unchanged files with a
GitNexus-confirmed caller, callee, or affected-process relationship. Changed
files, generic utilities, import-only links, and unverified relationships are
excluded. Every entry includes one reason and a verified `file:line` reference.

## Project policy

Add `AI_REVIEW.md` to a repository's default branch to describe operational
risks and domain-specific review priorities. The reviewer always reads it from
the PR's **base commit**:

```text
base commit AI_REVIEW.md  -> current review policy
PR changes AI_REVIEW.md   -> ignored for this review
merged AI_REVIEW.md       -> applies to later PRs
```

This prevents a PR from changing the instructions used to review itself.

## Evidence boundary and future inputs

The context contains the read-only repository, GitNexus code graph, diff, base policy, and
SHA-bound evidence from isolated CI. v0.1 implements Django check, migration
check, and pytest. Future versions can add normalized Ruff and Semgrep results.
It does not add a provider interface, web server, database, or analysis framework.

## Development

```bash
python -m pip install -e ".[dev]"
pytest
```

The tests mock Git, Codex, and Gitea. They do not require network access,
credentials, or a running Gitea instance.

## References

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [GitNexus](https://github.com/nxpatterns/gitnexus)
- [Gitea Actions](https://docs.gitea.com/usage/actions/)
- [Gitea Actions security guidance](https://docs.gitea.com/usage/actions/overview/)
