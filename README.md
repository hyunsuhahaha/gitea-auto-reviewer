# gitea-auto-reviewer

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![stdlib only](https://img.shields.io/badge/runtime%20deps-stdlib%20only-informational.svg)](pyproject.toml)

**English** · [한국어](README.ko.md)

**A self-hosted Gitea and Forgejo pull-request reviewer that proves, by running the code, whether a
Django PR breaks data integrity and how far the change spreads.**

It does not post whatever an LLM thinks after reading a diff. Impact is traced by combining a static
call graph with Django's runtime structure and the call paths that the test suite actually executes.
Data-integrity problems are confirmed by **running the same scenario on the base code and on the PR
head and comparing the database rows each run changes**. A fixed runner, not the LLM, makes the call.

## Features

| | What it does | Why |
|---|---|---|
| **Impact paths** | Combines the GitNexus static graph with Django signals, URLs, and model metadata, string-based ORM field references, and pytest call traces, then flags paths the static graph misses | `post_save` receivers and `filter(stock__lt=5)` are not calls, so call graphs do not see them |
| **Base vs head differential runs** | Runs each reproduction scenario twice on base and twice on head and compares row-level DB changes, return values, and exceptions just before rollback | If an LLM guesses the "expected" value, the verdict inherits the guess. The baseline is the real behavior of the code before the PR |
| **Rollback-only reproduction** | Every run happens inside `transaction.atomic()`, is forced to roll back, and every written table is checked again on a fresh connection | Uses a real test database without leaving data behind |
| **Change-aware test selection** | Runs only the tests a change can reach, using a base-branch map of the functions each test really executed and a function-level AST comparison, and balances parallel shards by recorded durations | Running the whole suite for a one-line change slows every review. Changes function coverage cannot bound fall back to the full suite |
| **Per-stage credentials** | Running PR code, the Codex review, and posting to Gitea happen in separate processes with separate credentials, and every input is bound to the head SHA | The code under review cannot reach the reviewer's tokens or policy |

## Example comment

This is the end-to-end CLI output for a PR against the sample app in
[`tests/fixtures/shop`](tests/fixtures/shop) that removes the "reject orders larger than the stock"
check. The reproduction plan and second-review JSON were written by hand instead of by Codex; evidence
collection, impact analysis, reproduction, and the verdict ran on real Django, SQLite, and GitNexus.

```text
Reproduced problems
  • An order larger than the stock is accepted and Product.stock goes negative
    Impact: stock no longer matches inventory and an oversold order is created
    Conditions used
      1. order an existing product with more than the available stock
    Base vs head differential run (2 runs each, run-specific values excluded)
      Product(pk=1).stock  base unchanged · head 1 → -2
    Other differences from base
      exception  base OutOfStock: widget · head none
      return value  base none · head {"quantity": 3}
      Order created  base 0 rows · head 1 rows (product_id=1, quantity=3, status="placed")
      OrderLog created  base 0 rows · head 1 rows (order_id=<new:Order#1>, message="ordered 3")
    Executed: shop/services.py:12
    Rollback check: passed

Impact paths
※ Found from Django structure, ORM field references, and pytest call traces · ⚠ absent from the GitNexus static graph
  • [ORM field] shop/services.py:place_order → shop/reports.py:low_stock_products ⚠
      Product.stock read — shop/reports.py:9
  • [ORM field] shop/services.py:place_order → shop/reports.py:placed_quantities ⚠
      Order.quantity read — shop/reports.py:5
  • [signal] shop/services.py:place_order → shop/signals.py:log_order ⚠
      Order post_save · confirmed by pytest — shop/signals.py:7
  • [runtime caller] shop/views.py:order_view → shop/services.py:place_order
      call chain entry shop/orders_spec.py:test_order_view_logs_order_and_decrements_stock
```

GitNexus found the `order_view → place_order` call, but not the two report functions that read the
stock or the signal receiver that runs when an order is created. The top of the comment also lists
the changed files, schema and data-processing changes, Django check, migration check, and pytest
results, and a risk level. Comments are in Korean by default; the example above uses
`language = "en"`.

## Pipeline

```text
PR head (pinned SHA)
 │
 ├─ test-select pick the tests to run from the base-branch impact map and git objects (before PR code runs)
 ├─ index      index the exact head SHA with GitNexus
 ├─ evidence   Django check · migration check · selected pytest in parallel shards (+ call tracing)
 ├─ impact     Django structure · ORM field references · runtime traces → compared with GitNexus
 │                                   ── PR code runs up to here, without credentials ──
 ├─ review     first Codex + GitNexus MCP pass (read-only sandbox, no Gitea token)
 ├─ plan       Codex writes a reproduction scenario per finding (differential / assert)
 ├─ reproduce  rollback runs on a base worktree and on head → the fixed runner decides
 ├─ verify     adversarial Codex pass: may only accept, or reject with a reason
 ├─ finalize   classify findings by reproduction outcome
 └─ comment    create/update the Gitea comment (no Codex, no PR code, Gitea token only)
```

Every stage runs in order inside one Actions job. The Linux workflow
([`examples/workflows/ai-review-linux.yml`](examples/workflows/ai-review-linux.yml), Docker job
container) and the Windows workflow ([`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml))
run the same steps in the same order.

### 1. Impact paths (`impact`)

Changed functions are found by mapping `git diff -U0 base head` hunks onto AST function ranges, by
Python qualname. Paths are then collected from three sources.

| Source | How | Paths found |
|---|---|---|
| Django structure | `django.setup()` in the project interpreter, then read the app registry | `pre_/post_save`·`delete` receivers of models the changed function writes, every write that triggers those receivers, URL entry points of changed views |
| ORM field references | resolve `filter/values/update/create`, `F()`, and `Q()` arguments of `Model.objects…` chains to model fields with the AST | other functions that read or write the fields the changed function writes, every function that references a field whose definition changed |
| Runtime traces | a plugin in the existing pytest run records the call stack on entry to a changed function with `sys.setprofile` | paths and tests that really called the changed function, project functions called while it ran |

A path found by both Django structure and runtime traces is merged and marked `confirmed by pytest`.
Finally the GitNexus MCP server is queried over STDIO for upstream/downstream `impact` of every changed
function, and paths missing from its answer get ⚠. The result goes into the first Codex prompt as
deterministic evidence and is rendered in the comment's `Impact paths` section.

### 2. First pass (`review`)

Codex reads the diff, the full head repository, the GitNexus graph, CI evidence, impact paths, and the
`AI_REVIEW.md` policy from the base commit, and proposes at most five findings. The program checks the
JSONL event stream to confirm GitNexus `detect_changes`·`context`·`impact` calls really completed, and
checks that every `file:line` citation and policy quote exists. Changed-file counts, CI results, and
impact paths are fixed fields that overwrite model output.

### 3. Reproduction and verdict (`plan` → `reproduce`)

Codex only proposes a reproduction scenario (a script with a single `def reproduce():`) and how to
judge it; it never decides the result. Scripts are checked with the AST to block file, process, and
network access and transaction control before they run.

**Differential mode** — the default for data-integrity findings.

1. The PR's merge base (the same baseline as the review stage's `base...head` diff) is checked out as
   a temporary `git worktree`, so later movement of the base branch is ignored.
2. The same script runs twice on head and twice on base. Each run installs an SQL execute wrapper
   inside an outer `atomic` that snapshots a table the first time it is written. When the scenario
   ends, the created, updated, and deleted rows are computed just before rollback.
3. Values that differ between runs are normalized before comparison.
   - Primary keys of new rows are dropped, because PostgreSQL sequences do not roll back.
   - Foreign keys to new rows become creation-order placeholders such as `<new:Order#1>`.
   - `auto_now` and `auto_now_add` values are excluded.
   - Anything that differs between two runs of the same side (uuids, current time) is excluded.
4. Verdicts:

| Observation | Verdict |
|---|---|
| base and head DB changes, return values, and exceptions are identical | `refuted` — the PR did not cause it |
| a difference where the finding predicted (`Product.stock`, `result`, `exception`, …) | `confirmed` |
| differences only elsewhere | `refuted` — the observed differences are kept as the reason |
| head never reached the changed code, the entry point is missing on base, or the run failed | `inconclusive` |

Predicted locations are fixed in the plan, before anything runs, so the verdict cannot be fitted to
what was observed. A PR that changes migrations or dependency files (`requirements*.txt`,
`pyproject.toml`, …) cannot run base on the same test DB and venv, so that reason is passed to the
plan prompt and assert mode is used instead.

**Assert mode** — for findings that are not observable as a base/head behavior difference. The runner
compares the `expected` and `observed` values the script returns.

Both modes confirm with `sys.settrace` that the finding's Python evidence lines (±3 lines) really
executed. A script that fails before a verdict is sent back to Codex once, with the error, for an
automatic repair and rerun. When possible, the share of untouched rows that meet the bug condition is
also counted, such as `Bug condition rate: packing buckets 467/2,481 rows (18.82%)`. It is
informational only and never decides a verdict.

### 4. Second review and publishing (`verify` → `finalize` → `comment`)

An independent Codex pass tries to disprove each confirmed item. It may only accept an item, or
reject it with a concrete reason in the configured language; it can never add or rewrite items. The
comment is split into:

- `Reproduced problems`: reached the changed code, showed the difference, passed rollback and the second review
- `Findings not reproduced`: not run, inconclusive, or not reproduced (with the reason)
- `Reproduced but rejected on second review`: reproduced, then rejected by the second review (with the reason)

Findings from static analysis are never dropped just because reproduction failed. The comment is
found and updated through the `<!-- gitea-auto-reviewer:pr=42:sha=… -->` marker, and a rerun on the
same SHA keeps items reproduced earlier. After a new push, problems reproduced for the first time are
marked `[new]`, and problems reproduced on the previous commit but not confirmed again are listed once
more under `Reproduced in the previous commit (abc1234)`. Items with the same wording or the same
evidence locations count as the same problem.

Hidden state is read only from comments written by the bot account itself, so nobody can inject
`Reproduced problems` by copying the marker. Right before posting, the current PR head SHA is fetched
again and nothing is posted if a newer commit arrived during the review. The workflows also set a
per-PR `concurrency` group that cancels superseded runs, and a 120-minute `timeout-minutes`.

### Change-aware test selection and sharding (`test-select`)

Opt in with `[tests].selection = true`. While it is off, the full suite runs once, as before.

**1. Recording the test impact map.** On every push to the base branch,
[`ai-review-test-map-linux.yml`](examples/workflows/ai-review-test-map-linux.yml) (Windows:
[`.gitea/workflows/ai-review-test-map.yml`](.gitea/workflows/ai-review-test-map.yml)) runs the full
suite once with `evidence --record-test-map`. The same pytest plugin uses `sys.setprofile` to record,
per test, the repository functions (by qualname) it really executed, its duration, and its outcome; the
map is saved to the Actions cache keyed by commit SHA. Because it records execution rather than a
static import graph, signal receivers and ORM callbacks are linked to the tests that trigger them.

- Session- and class-scoped fixtures and unittest `setUpClass`/`setUpTestData` run once, during the
  first test that needs them. Code run while a fixture is set up is therefore collected per fixture and
  attributed to every test whose fixture closure contains it.
- Functions that run outside any test (imports, collection, `AppConfig.ready`, conftest hooks) are
  marked separately. The plugin loads before conftest files, so it sees that phase too.

**2. Selection.** A PR review restores the map read-only and, before any PR code runs, compares the map
commit with the PR head at function granularity using git objects alone. Functions are compared as
ASTs without line numbers or docstrings, so moved lines and comment edits are not changes.

| Tests that run | Changes that fall back to the full suite |
|---|---|
| tests that executed a changed or removed function | module- or class-level code (constants, model fields, imports, adding or removing a method, which can change dispatch) |
| tests missing from the map (new or renamed) | a deleted or renamed Python file |
| every test in a changed test module | a changed function that runs outside tests (imports, collection) |
| tests that failed when the map was recorded | non-Python files (`requirements*.txt`, templates, SQL, ...; documentation and `ignore_paths` excepted) |
| | paths in `[tests].full_run_paths`; a missing or invalid map, or a map commit that is not an ancestor of the head |

The difference is computed from the **map commit** to the head, not from the base, so an older map
still accounts for everything merged since. The comment shows the scope under the test line, such as
`Ran 12 of 340 tests affected by this change` or `Ran all 340 tests: module- or class-level change in
shop/models.py`, and the job log lists changed functions that no existing test executes.

**3. Sharding.** With `[tests].shards = N`, the selected tests are split across N parallel pytest
processes using longest-processing-time-first packing of recorded durations; tests without a recorded
duration are estimated at the median. Tests of one class (or else one module) stay on one shard so
`setUpClass` and module fixtures are not repeated. Every shard computes the same deterministic
placement from the same collection, so each test runs exactly once without coordination. Each shard
gets `AI_REVIEW_SHARD_INDEX` and `AI_REVIEW_SHARD_COUNT`; with a shared database such as PostgreSQL,
add them to the test database name in your CI settings so shards do not collide.

```python
# config/settings/ci.py
DATABASES["default"]["TEST"] = {"NAME": f"test_app_{os.environ.get('AI_REVIEW_SHARD_INDEX', '0')}"}
```

### Skipped reviews

Automatic runs skip the following PRs through `metadata --allow-skip`, and every later step is
skipped too. Manual `workflow_dispatch` runs are never skipped.

- PRs labeled `skip-ai-review` (`[skip].label`; applies from the next push)
- draft PRs, or titles starting with `WIP:`, `[WIP]`, or `WIP ` (turn off with `[skip].drafts = false`)
- PRs whose changed files all match `[skip].documentation_paths` (default `*.md`, `*.rst`, `docs/*`,
  `LICENSE`; never when more than 1,000 files changed)

## Trust model

v0.3 supports only:

- a trusted, dedicated, self-hosted runner;
- same-repository PRs in private or internal repositories (no fork PRs);
- a version of this package you installed and audited in advance.

| Stage | Runs PR code | Codex auth | Gitea token |
|---|---|---|---|
| `evidence`, `impact`, `reproduce` | yes (allowlisted environment, temporary HOME) | no | no |
| `review`, `plan`, `verify` | no (`--sandbox read-only`, `--ephemeral`) | yes | no |
| `comment` | no | no | yes |

- Review policy (`AI_REVIEW.md`) and settings (`.ai-review.toml`) are read from the base commit, never
  the PR, so a PR cannot change its own review instructions.
- Repository files (including `AGENTS.md`), the diff, and CI output are treated as data, never as
  instructions.
- The base side of a differential run is a trusted, already-merged commit and does not widen the
  security boundary.
- The pytest call-trace plugin runs in the same process as PR code, so its output is trusted exactly
  as much as the pytest result.
- Only the base-branch push workflow records the test impact map and writes the cache. PR reviews read
  it with `actions/cache/restore`, and test selection finishes before any PR code runs. A map file
  committed by the PR at the map path is ignored and the full suite runs.
- This is process separation, not VM isolation. Do not use it on untrusted public repositories. The
  `read-only` sandbox blocks repository writes but not OS-level process execution.
- In the Docker job container, PR code and the Codex login volume run as the same container user. The
  container protects the runner host; between stages the credential boundary is process environment
  separation, exactly as on the Windows runner.

## Setup

### Quick start: Linux + Docker

You need a Gitea or Forgejo Actions runner that runs Docker job containers, a ChatGPT account with
Codex access, and a Django test database for reproductions.

1. On the runner host, build the image and log in to Codex once. The login is stored only in the
   `ai-review-codex` volume, never in the image.

   ```bash
   git clone https://github.com/hyunsuhahaha/gitea-auto-reviewer.git
   docker build -t gitea-auto-reviewer:0.3.0 gitea-auto-reviewer
   docker run --rm -it -v ai-review-codex:/codex-home gitea-auto-reviewer:0.3.0 codex login --device-auth
   ```

2. Label the runner `ai-review-linux` and allow the volume in the act_runner config.

   ```yaml
   container:
     valid_volumes: ["ai-review-codex"]
   ```

3. Create a dedicated bot account and store a token with only issue/PR comment read·write and
   `read:user` scopes as the `AI_REVIEW_GITEA_TOKEN` Actions secret. `read:user` identifies the bot's
   own comments; without it comments are still posted, but earlier review state is not carried over
   and `feedback` cannot run. Never grant merge or admin rights.
4. Commit two files to the base branch of the repository under review:
   - [`examples/workflows/ai-review-linux.yml`](examples/workflows/ai-review-linux.yml) →
     `.gitea/workflows/ai-review.yml` (Forgejo: `.forgejo/workflows/ai-review.yml`)
   - [`examples/ai-review.toml`](examples/ai-review.toml) → `.ai-review.toml`. Set `language = "en"`
     for English comments, and add the Django
     settings module that uses your test database and the safety switches to check before any
     reproduction.
5. CI dependencies are installed from the repository's `requirements-ci.txt`. Set the Actions
   variables `AI_REVIEW_REQUIREMENTS` and `AI_REVIEW_PYTHON_VERSION` to change the file or Python
   version, and `AI_REVIEW_IMAGE` to change the image name.
6. (Optional) To run only the tests a change affects, add
   [`examples/workflows/ai-review-test-map-linux.yml`](examples/workflows/ai-review-test-map-linux.yml)
   as `.gitea/workflows/ai-review-test-map.yml` and set `[tests] selection = true` in `.ai-review.toml`.
   The full suite runs until the first map is recorded.

Open a PR and the review comment appears. To review an existing or merged PR, run the workflow from
the Actions page (`workflow_dispatch`) with just the PR number.

### Windows runner

[`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml) is a Windows example that installs
everything directly in the runner account, without Docker. Install Python 3.11+, Git, the Codex CLI,
and the GitNexus CLI for the runner service account, and run `codex login` once as that account.
Adjust the paths in the workflow's top-level `env`. Never run `pip install .` in a PR checkout; build
hooks are executable code.

```bash
python -m pip install ./gitea-auto-reviewer uv
npm install -g gitnexus
codex login
```

### Forgejo

Forgejo serves the same `/api/v1` API as Gitea, so the CLI works unchanged, and the Linux workflow uses
only the `github.*` context that both Gitea and Forgejo provide. An end-to-end run on a real Forgejo
instance has not been verified yet; please open an issue if anything breaks.

### `.ai-review.toml`

The reviewer reads this file **only from the PR's base commit**, so a PR cannot loosen its own review
settings. Unknown keys, invalid values, and environment variable names that look like credentials
(containing `TOKEN`, `SECRET`, `PASSWORD`, `CODEX`, `GITEA`, …) are rejected.

```toml
language = "ko"                          # comment and Codex explanation language: ko (default) | en

[reasoning]                              # low | medium | high
first_pass = "medium"
plan = "medium"
verify = "low"
repair = "medium"

[skip]
label = "skip-ai-review"
drafts = true
documentation_paths = ["*.md", "*.rst", "docs/*", "LICENSE"]

[django]
settings_module = "config.settings.ci"   # settings that use the test database

[evidence]
pass_env = ["DATABASE_URL"]              # runner environment variables passed to PR code

[reproduction]
timeout = 180

[reproduction.required_settings]         # settings that must have these values before reproducing
PAYMENT_LIVE_MODE = false

[tests]
selection = true                         # run only the tests a change affects (default false)
shards = 4                               # parallel pytest shards, 1-16 (default 1)
full_run_paths = ["*/sql/*"]             # paths that always trigger the full suite
ignore_paths = ["deploy/*"]              # non-Python files that cannot affect tests
```

Without the file, defaults apply (Korean comments, default skip rules, no extra environment
variables). The workflows resolve the base commit's settings once with `metadata --config-output`, and
later steps read that JSON through the `AI_REVIEW_CONFIG_FILE` environment variable. Locally,
`gitea-auto-reviewer config --base-sha <SHA>` writes the same JSON. Reasoning effort resolves in this
order: CLI option, Actions variable (`AI_REVIEW_FIRST_PASS_EFFORT`, `AI_REVIEW_PLAN_EFFORT`,
`AI_REVIEW_VERIFY_EFFORT`, `AI_REVIEW_REPAIR_EFFORT`), config file, default. `--require-setting` adds to
the settings from the config file.

## CLI

| Command | Purpose | Main options |
|---|---|---|
| `metadata` | resolve PR number, title, and base/head SHAs from Gitea; decide whether to skip | `--pr`, `--output-file`, `--allow-skip`, `--config-output` |
| `config` | resolve the base commit's `.ai-review.toml` to JSON | `--base-sha`, `--output` |
| `index` | index the head SHA with GitNexus | `--head-sha` |
| `test-select` | choose the tests to run from the test impact map and git objects | `--test-map`, `--output` |
| `evidence` | Django check, migration check, pytest | `--only`, `--trace-output`, `--selection-file`, `--shards`, `--record-test-map` |
| `evidence-merge` | combine per-check evidence | `--input` (repeatable) |
| `impact` | compute impact paths and compare with GitNexus | `--runtime-trace`, `--skip-gitnexus` |
| `review` | first Codex pass | `--evidence-file`, `--impact-file` |
| `plan` | plan reproduction scenarios | `--base-sha` (assert mode only without it) |
| `reproduce` | rollback reproduction and verdict | `--base-sha`, `--require-setting NAME=value` |
| `verify` | adversarial second review | `--reproduction-file` |
| `finalize` | classify findings for publishing | `--verification-file` |
| `comment` | create/update the Gitea comment (skipped if the head moved) | token only through the `GITEA_REVIEW_TOKEN` environment variable |
| `feedback` | count 👍/👎 reactions and findings on recent bot comments | `--limit`, `--output` |
| `reasoning` | print the effective reasoning efforts | |

Commands that read or run the repository (`index`, `evidence`, `impact`, `review`, `reproduce`) refuse
to run when the real `HEAD` differs from `--head-sha`. Every JSON file passed between stages carries
the head SHA, and files for another SHA are rejected. The workflow files show complete invocations.

`feedback` uses only the Gitea token and never runs Codex or PR code. `feedback.json` lists
`reproduced`, `unreproduced`, `rejected`, `thumbs_up`, and `thumbs_down` per PR plus totals, so you can
compare reaction rates before and after changing prompts or reasoning effort. Reactions apply to the
whole comment, not to individual findings.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

- [`tests/fixtures/shop`](tests/fixtures/shop) is a sample Django app used as the base commit;
  overlaying [`tests/fixtures/shop_head`](tests/fixtures/shop_head) makes the head commit. Differential
  and impact tests build a temporary git repository from those two commits and run real Django on
  SQLite.
- With `gitnexus` on PATH, extra tests verify the static-graph comparison against a real GitNexus
  index and MCP server.
- Codex and the Gitea API are mocked in tests.

## Known limitations

- ORM field references are resolved only for chains starting with `Model.objects…`. Related managers
  (`product.orders.filter(…)`) and instance attribute assignment are not recognized yet.
- DRF serializer `source=` and Celery task paths are not collected yet.
- Call-chain tracing records only the main thread. The test impact map also records other threads,
  but not code run in subprocesses a test starts.
- Test selection assumes deterministic tests. A test that takes different paths between runs is
  linked only to the functions it executed when the map was recorded.
- PRs that change migrations or dependencies are judged in assert mode instead of differential mode.
- Differential runs compare tables with more than 5,000 rows partially; only newly created rows are
  compared then.
- The Docker image and the Forgejo workflow are not yet exercised end to end in CI.

## References

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) · [Codex authentication](https://learn.chatgpt.com/docs/auth) · [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [GitNexus](https://github.com/abhigyanpatwari/GitNexus)
- [Gitea Actions](https://docs.gitea.com/usage/actions/) · [Forgejo Actions](https://forgejo.org/docs/latest/user/actions/)
