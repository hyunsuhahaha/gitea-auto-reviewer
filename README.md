# gitea-auto-reviewer

`gitea-auto-reviewer` is a compact AI change-impact summary for self-hosted
Gitea. It connects an existing Codex subscription to pull-request comments and
explains what changed, what new assumptions the change introduces, where it may
have an operational impact, and what a person should verify. It does not require
an OpenAI API key or a Copilot seat.

It is an AI-assisted first-pass reviewer, not a merge gate. It can create or
update one ordinary PR comment. It cannot approve, request changes, merge,
push commits, or modify branch protection.

## v0.1 flow

```text
PR head ── isolated, credential-free CI sandbox
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
                          │
                     review.json
                          │
                    comment process
                    Codex auth: unused
                    Gitea token: yes
                          │
                 create/update PR comment
```

The three CLI stages are intentionally separate:

```bash
gitea-auto-reviewer evidence ... # isolated PR execution, no Codex/Gitea credential
gitea-auto-reviewer review ...   # Codex session, consumes evidence, no Gitea credential
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

v0.1 intentionally supports only:

- trusted, persistent, self-hosted runners;
- private/internal repositories;
- PRs whose head and base belong to the same repository;
- a preinstalled, audited version of this package;
- a normal Markdown timeline comment;
- a maximum diff size of 1 MB by default.

Only the isolated evidence job executes PR code. Codex is never asked to run
tests, builds, migrations, package managers, or project scripts. The evidence
job must use an ephemeral, non-privileged sandbox with no secrets or network.
Only the PR checkout and evidence output directory are mounted; no credential,
Docker socket, or unrelated host path enters the sandbox. It does not auto-fix code and a finding never changes the workflow
result solely because of its severity.

Do not use this version for fork PRs or untrusted public repositories. A
self-hosted runner is privileged infrastructure; Gitea itself recommends that
runners and repositories trust each other.

## Requirements

- Python 3.11 or newer
- Git
- Codex CLI with `--output-schema` support
- A persistent self-hosted Gitea Actions runner
- A ChatGPT account entitled to use Codex
- A Gitea token that can read and write PR/issue comments

## Install on the runner

Install one audited release as the operating-system account used by the runner.
Do this during runner provisioning, not from a pull request:

```bash
python -m pip install "gitea-auto-reviewer==0.1.0"
```

For development before a package is published:

```bash
git clone https://github.com/hyunsuhahaha/gitea-auto-reviewer.git
cd gitea-auto-reviewer
python -m pip install .
```

Do not run `pip install .` against an untrusted PR checkout. Python build hooks
are executable code.

## Authenticate Codex without an API key

Sign in once, interactively, as the same OS account that runs the persistent
runner:

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
5. Ensure `gitea-auto-reviewer` and `codex` are available on the runner `PATH`.

The example uses `pull_request_target` so the workflow definition comes from
   the trusted base branch. It checks that the PR comes from the same repository,
   checks out the PR head for inspection, and never invokes code from that checkout.

## CLI

Collect deterministic evidence inside the isolated PR execution sandbox:

```bash
gitea-auto-reviewer evidence \
  --head-sha 2222222222222222222222222222222222222222 \
  --repo-dir /workspace/payments \
  --output /artifacts/evidence.json
```

This runs `python manage.py check`, `python manage.py makemigrations --check
--dry-run`, and `python -m pytest -q -p no:cacheprovider`. It verifies that the checkout's actual
`HEAD` equals `--head-sha` before executing anything and records that SHA in the
evidence document.

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
  --output /runner/temp/review.json
```

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
DB 변경          있음
API Contract     변경
외부연동         영향 가능

검증된 사실
✅ Django check PASS
✅ Migration check 누락 없음
✅ 테스트(pytest) 147/147 PASS

위험도           🟠 HIGH

주요 변경
• Product.remark 추가
• 상품 등록·조회 API 변경

새롭게 생긴 가정
• 모든 Product 생성 경로가 remark를 처리해야 함
• SCM/ERP가 변경된 payload를 처리할 수 있어야 함

주의
• SCM 동기화 코드에서 기존 Product 생성 경로 발견

확인 필요
□ remark 없는 기존 상품 생성
□ SCM/ERP payload 호환성
□ migration 적용 및 rollback
```

The program validates the structured result before posting it. The changed-file
count comes from Git. Django, migration, and pytest results come from the
evidence document and overwrite model output. Evidence whose `head_sha` does
not exactly match the reviewed PR head is rejected. A hidden marker lets later
runs update the existing comment without a database:

```html
<!-- gitea-auto-reviewer:pr=42:sha=abc123... -->
```

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

The context contains the read-only repository, diff, base policy, and
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
- [Gitea Actions](https://docs.gitea.com/usage/actions/)
- [Gitea Actions security guidance](https://docs.gitea.com/usage/actions/overview/)
