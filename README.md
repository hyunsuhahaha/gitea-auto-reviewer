# gitea-auto-reviewer

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![stdlib only](https://img.shields.io/badge/runtime%20deps-stdlib%20only-informational.svg)](pyproject.toml)

**Django 프로젝트의 PR이 데이터 정합성을 깨는지, 그리고 그 변경이 어디까지 퍼지는지를
실제 실행으로 검증하는 셀프 호스팅 Gitea PR 리뷰어입니다.**

LLM이 diff를 읽고 쓴 의견을 그대로 게시하지 않습니다. 영향 범위는 정적 호출 그래프에
Django 런타임 구조와 실제 테스트 실행 경로를 더해 찾고, 데이터 정합성 문제는 같은
시나리오를 **base 코드와 PR head 코드에서 각각 실행해 DB 행 변경을 비교**하는 방식으로
확인합니다. 판정은 LLM이 아니라 고정된 실행기가 내립니다.

## 핵심 기능

| | 무엇을 하는가 | 왜 필요한가 |
|---|---|---|
| **영향 경로 분석** | GitNexus 정적 그래프에 Django signal·URL·모델 메타데이터, 문자열 ORM 필드 참조, pytest 호출 추적을 합쳐 변경 함수의 영향 경로를 계산하고, 정적 그래프에 없는 경로를 표시 | `post_save` receiver, `filter(stock__lt=5)` 같은 경로는 호출 관계가 아니라서 호출 그래프에 나타나지 않음 |
| **base·head 차분 실행** | 같은 재현 시나리오를 base와 head에서 각 2회 실행하고, 롤백 직전의 행 단위 DB 변경·반환값·예외를 비교 | LLM이 "기대값"을 추측하면 판정도 함께 틀림. 기준을 "PR 이전 코드의 실제 동작"으로 고정 |
| **롤백 전용 재현** | 모든 실행을 `transaction.atomic()` 안에서 강제 롤백하고, 새 연결로 쓰기가 일어난 모든 테이블이 원상태인지 검사 | 실제 테스트 DB를 쓰면서도 데이터를 남기지 않음 |
| **단계별 자격 증명 분리** | PR 코드 실행, Codex 리뷰, Gitea 댓글 작성을 서로 다른 프로세스와 자격 증명으로 분리하고 모든 입력을 head SHA에 고정 | 리뷰 대상 코드가 리뷰어의 토큰이나 정책을 건드리지 못하게 함 |

## 리뷰 댓글 예시

[`tests/fixtures/shop`](tests/fixtures/shop)의 예제 앱에서 "주문 수량이 재고보다 많으면
거절하는 검사"를 제거한 PR을 CLI로 끝까지 실행한 결과입니다. 재현 계획과 2차 검증
JSON은 Codex 대신 손으로 작성해 넣었고, 나머지 증거 수집·영향 분석·재현·판정 단계는
실제 Django, SQLite, GitNexus에서 실행됐습니다.

```text
재현된 문제
  • 재고보다 많은 주문이 승인되어 Product.stock이 음수가 됨
    영향: 재고 수량이 실제 보유량과 달라지고 초과 주문이 생성됨
    재현에 사용한 조건
      1. 보유 재고보다 많은 수량으로 기존 상품 주문
    base·head 차분 실행 (각 2회, 실행마다 달라지는 값 제외)
      Product(pk=1).stock  base 변경 없음 · head 1 → -2
    그 밖의 base 대비 차이
      예외  base OutOfStock: widget · head 없음
      반환값  base 없음 · head {"quantity": 3}
      Order 생성  base 0건 · head 1건 (product_id=1, quantity=3, status="placed")
      OrderLog 생성  base 0건 · head 1건 (order_id=<new:Order#1>, message="ordered 3")
    실행 도달: shop/services.py:12
    롤백 검증: 통과

영향 경로
※ Django 구조·ORM 필드 참조·pytest 실행 추적으로 찾은 경로 · ⚠ GitNexus 정적 그래프에 없는 경로
  • [ORM 필드] shop/services.py:place_order → shop/reports.py:low_stock_products ⚠
      Product.stock 읽기 — shop/reports.py:9
  • [ORM 필드] shop/services.py:place_order → shop/reports.py:placed_quantities ⚠
      Order.quantity 읽기 — shop/reports.py:5
  • [signal] shop/services.py:place_order → shop/signals.py:log_order ⚠
      Order post_save · pytest 실행으로 확인 — shop/signals.py:7
  • [실행 호출자] shop/views.py:order_view → shop/services.py:place_order
      실행 체인 진입점 shop/orders_spec.py:test_order_view_logs_order_and_decrements_stock
```

GitNexus는 `order_view → place_order` 호출은 찾았지만, 재고를 읽는 보고서 함수 두 개와
주문 생성 시 실행되는 signal receiver는 찾지 못했습니다. 댓글 상단에는 변경 파일, DB
스키마·데이터 처리 변경, Django check·마이그레이션·pytest 결과, 위험도가 함께 표시됩니다.

## 파이프라인

```text
PR head (SHA 고정)
 │
 ├─ index      GitNexus로 정확한 head SHA 인덱싱
 ├─ evidence   Django check · migration check · pytest (+ 변경 함수 호출 추적)
 ├─ impact     Django 구조 · ORM 필드 참조 · 실행 추적 → GitNexus와 비교
 │                                   ── 여기까지 PR 코드 실행, 자격 증명 없음 ──
 ├─ review     Codex + GitNexus MCP 1차 분석 (읽기 전용 샌드박스, Gitea 토큰 없음)
 ├─ plan       Codex가 finding별 재현 시나리오 작성 (차분 / 단정 모드)
 ├─ reproduce  base worktree와 head에서 롤백 실행 → 고정 실행기가 판정
 ├─ verify     Codex 반증 단계: 확정 항목을 채택하거나 사유와 함께 거절만 가능
 ├─ finalize   재현 결과에 따라 게시 분류
 └─ comment    Gitea 댓글 생성/갱신 (Codex·PR 코드 실행 없음, Gitea 토큰만 보유)
```

모든 단계는 [`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml)에서 하나의
전용 Windows 러너 작업으로 순차 실행됩니다.

### 1. 영향 경로 분석 (`impact`)

변경된 함수는 `git diff -U0 base head`의 hunk를 AST 함수 범위에 대응시켜 Python
qualname 단위로 찾습니다. 그다음 세 출처에서 경로를 모읍니다.

| 출처 | 수집 방법 | 찾는 경로 |
|---|---|---|
| Django 구조 | 프로젝트 인터프리터로 `django.setup()` 후 앱 레지스트리를 읽음 | 변경 함수가 쓰는 모델의 `pre_/post_save`·`delete` receiver, receiver를 트리거하는 모든 쓰기 위치, 변경된 view의 URL 진입점 |
| ORM 필드 참조 | AST에서 `Model.objects…` 체인의 `filter/values/update/create`, `F()`, `Q()` 인자를 모델 필드로 해석 | 변경 함수가 쓰는 필드를 읽거나 쓰는 다른 함수, 정의가 바뀐 필드를 참조하는 모든 함수 |
| 실행 추적 | 기존 pytest 실행에 플러그인을 로드해 `sys.setprofile`로 변경 함수 진입 시 호출 스택 기록 | 실제로 변경 함수를 호출한 경로와 테스트, 변경 함수 실행 중 호출된 프로젝트 함수 |

같은 경로가 Django 구조와 실행 추적 양쪽에서 나오면 하나로 합치고
`pytest 실행으로 확인`을 붙입니다. 마지막으로 GitNexus MCP 서버를 직접 STDIO로 호출해
변경 함수마다 upstream/downstream `impact`를 조회하고, 그 결과에 없는 경로에 ⚠를
표시합니다. 결과는 Codex 1차 리뷰 프롬프트에 결정론적 증거로 들어가고, 댓글의
`영향 경로` 섹션에 그대로 렌더링됩니다.

### 2. 1차 리뷰 (`review`)

Codex는 diff, 전체 head 저장소, GitNexus 그래프, CI 증거, 영향 경로, base 커밋의
`AI_REVIEW.md` 정책을 함께 읽고 finding 후보를 최대 5개 만듭니다. 프로그램은 GitNexus
`detect_changes`·`context`·`impact` 호출이 실제로 완료됐는지 JSONL 이벤트로 검사하고,
모든 `file:line` 근거와 정책 인용이 실제로 존재하는지 검증합니다. 변경 파일 수, CI
결과, 영향 경로는 모델 출력을 덮어쓰는 고정 필드입니다.

### 3. 재현과 판정 (`plan` → `reproduce`)

Codex는 재현 시나리오(`def reproduce():` 하나만 있는 스크립트)와 판정 방식을 제안할 뿐,
결과를 판정하지 않습니다. 스크립트는 AST 검사로 파일·프로세스·네트워크 접근과 트랜잭션
조작을 차단한 뒤 실행합니다.

**차분 모드 (`differential`)** — 데이터 정합성 finding의 기본값입니다.

1. PR의 merge base(리뷰 단계의 `base...head` diff와 같은 기준)를 임시 `git worktree`로
   꺼냅니다. base 브랜치가 그 뒤에 움직였어도 PR 자체의 변경만 비교합니다.
2. head와 base에서 같은 스크립트를 각 2회 실행합니다. 각 실행은 외부 `atomic` 안에서
   SQL execute wrapper를 설치해, 테이블에 처음 쓰기가 일어나는 순간 해당 테이블을
   스냅샷합니다. 시나리오가 끝나면 롤백 직전에 생성·수정·삭제된 행을 계산합니다.
3. 비교 전에 실행마다 달라지는 값을 정규화합니다.
   - 새로 생성된 행의 pk는 제거합니다. PostgreSQL 시퀀스는 롤백되지 않기 때문입니다.
   - 새 행을 가리키는 FK는 `<new:Order#1>`처럼 생성 순서로 바꿉니다.
   - `auto_now`·`auto_now_add` 값은 제외합니다.
   - 같은 쪽의 두 실행끼리 값이 다른 항목(uuid, 현재 시각 등)은 비교에서 뺍니다.
4. 판정 규칙은 다음과 같습니다.

| 관찰 | 판정 |
|---|---|
| base와 head의 DB 변경·반환값·예외가 모두 같음 | `refuted` — PR이 만든 문제가 아님 |
| finding이 예측한 위치(`Product.stock`, `result`, `exception` 등)에서 차이 | `confirmed` |
| 예측하지 않은 위치에서만 차이 | `refuted` — 관찰된 차이를 사유로 남김 |
| head에서 변경 근거 코드 미도달, base에 진입점 없음, 실행 오류 | `inconclusive` |

예측 위치는 실행 전에 plan 단계에서 고정합니다. 실행 후 관찰된 차이에 맞춰 판정을
바꿀 수 없습니다. 마이그레이션이나 의존성 파일(`requirements*.txt`, `pyproject.toml`
등)이 바뀐 PR은 같은 테스트 DB와 venv로 base를 실행할 수 없으므로, 그 사유를 plan
프롬프트에 전달하고 단정 모드로 전환합니다.

**단정 모드 (`assert`)** — base와의 동작 차이로 관찰할 수 없는 finding에 씁니다.
스크립트가 돌려준 `expected`와 `observed`를 실행기가 비교합니다.

두 모드 모두 finding의 Python 근거 라인(±3줄)이 실제로 실행됐는지 `sys.settrace`로
확인합니다. 판정 전에 실패한 스크립트는 오류 증거와 함께 Codex에 돌려보내 한 번만
자동 수정 후 재실행합니다. 가능한 경우 롤백 전 원본 데이터에서
`버그 조건 충족률: 포장 투입 버킷 467/2,481건 (18.82%)` 같은 비율도 집계합니다.
이 비율은 정보 제공용이며 판정에는 쓰지 않습니다.

### 4. 반증과 게시 (`verify` → `finalize` → `comment`)

독립된 Codex 단계가 확정된 항목을 반증하려 시도합니다. 이 단계는 항목을 채택하거나
구체적인 한국어 사유와 함께 거절할 수만 있고, 항목을 추가하거나 고쳐 쓸 수는 없습니다.
최종 댓글은 다음과 같이 나뉩니다.

- `재현된 문제`: 코드 도달, 차이 관찰, 롤백 검증, 반증 단계를 모두 통과한 항목
- `재현하지 못한 발견 사항`: 미실행, 실행 불확정, 실행상 미재현 항목 (사유 포함)
- `재현 성공 후 2차 검증 미채택`: 재현은 됐지만 반증 단계에서 거절된 항목 (사유 포함)

정적 분석 단계에서 나온 finding은 재현에 실패했다는 이유만으로 삭제하지 않습니다.
댓글은 `<!-- gitea-auto-reviewer:pr=42:sha=… -->` 마커로 찾아 갱신하고, 같은 SHA에서
다시 실행하면 이전에 재현된 항목을 보존합니다.

## 신뢰 모델

v0.3은 다음 환경만 지원합니다.

- 신뢰할 수 있는 전용 셀프 호스팅 러너
- 비공개 또는 내부 저장소의 같은 저장소 PR (fork PR 미지원)
- 사전에 설치하고 감사한 이 패키지 버전

| 단계 | PR 코드 실행 | Codex 인증 | Gitea 토큰 |
|---|---|---|---|
| `evidence`, `impact`, `reproduce` | 예 (허용 목록 환경 변수, 임시 HOME) | 없음 | 없음 |
| `review`, `plan`, `verify` | 아니요 (`--sandbox read-only`, `--ephemeral`) | 있음 | 없음 |
| `comment` | 아니요 | 없음 | 있음 |

- 리뷰 정책은 PR이 아니라 base 커밋의 `AI_REVIEW.md`에서 읽습니다. PR이 자기 리뷰 지침을
  바꿀 수 없습니다.
- 저장소 파일(`AGENTS.md` 포함), diff, CI 출력은 모두 지시가 아닌 데이터로 취급합니다.
- 차분 실행의 base 쪽은 이미 병합된 신뢰된 커밋이라 보안 경계를 넓히지 않습니다.
- pytest 호출 추적 플러그인은 PR 코드와 같은 프로세스에서 돌기 때문에, 그 결과는
  pytest 결과와 같은 신뢰 수준으로 취급합니다.
- 이것은 프로세스 분리이지 VM 격리가 아닙니다. 신뢰할 수 없는 공개 저장소에는 쓰지
  마세요. `read-only` 샌드박스는 저장소 쓰기를 막지만 OS 수준의 프로세스 실행까지 막지는
  않습니다.

## 설치와 설정

요구 사항: Python 3.11+, Git, `exec --json`과 `--output-schema`를 지원하는 Codex CLI,
GitNexus CLI, `ai-review-windows` 라벨이 붙은 전용 Gitea 러너.

1. 러너 서비스 계정에 감사가 끝난 버전을 설치합니다. PR 체크아웃에서 `pip install .`을
   실행하지 마세요. 빌드 훅도 실행 가능한 코드입니다.

   ```bash
   git clone https://github.com/hyunsuhahaha/gitea-auto-reviewer.git
   python -m pip install ./gitea-auto-reviewer uv
   npm install -g gitnexus
   ```

2. 같은 OS 계정으로 Codex에 한 번 로그인합니다. **Sign in with ChatGPT**를 선택하면
   `OPENAI_API_KEY` 없이 ChatGPT 계정의 Codex 권한을 사용합니다.

   ```bash
   codex login
   ```

3. Gitea에 전용 봇 계정을 만들고, 이슈/PR 댓글 조회·작성·수정 권한만 가진 토큰을
   `AI_REVIEW_GITEA_TOKEN` Actions 비밀 정보로 저장합니다.
4. [`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml)을 리뷰 대상
   저장소의 base 브랜치에 복사합니다. 워크플로는 `pull_request_target`으로 base의
   정의를 사용하고, 각 PR마다 새 `.venv-ci`를 만듭니다.
5. 재현용 테스트 DB 연결은 저장소에 추적되는 설정 파일이나 허용 목록 환경 변수로
   지정합니다. base worktree에는 추적되지 않는 로컬 설정 파일이 없습니다.

Actions 변수 `AI_REVIEW_FIRST_PASS_EFFORT`, `AI_REVIEW_PLAN_EFFORT`(기본 `medium`),
`AI_REVIEW_VERIFY_EFFORT`(기본 `low`)로 Codex 추론 수준을 바꿀 수 있습니다. 기존 PR이나
병합된 PR은 Actions 화면의 `workflow_dispatch`에 PR 번호만 넣어 다시 리뷰할 수 있습니다.

## CLI

| 명령 | 역할 | 주요 옵션 |
|---|---|---|
| `metadata` | Gitea에서 PR 번호·제목·base/head SHA 조회 | `--pr`, `--output-file` |
| `index` | head SHA를 GitNexus로 인덱싱 | `--head-sha` |
| `evidence` | Django check, migration check, pytest 실행 | `--only`, `--trace-output` |
| `evidence-merge` | 단계별 증거를 하나로 합침 | `--input` (반복) |
| `impact` | 영향 경로 계산과 GitNexus 비교 | `--runtime-trace`, `--skip-gitnexus` |
| `review` | Codex 1차 리뷰 | `--evidence-file`, `--impact-file` |
| `plan` | 재현 시나리오 계획 | `--base-sha` (없으면 단정 모드만) |
| `reproduce` | 롤백 재현과 판정 | `--base-sha`, `--require-setting NAME=value` |
| `verify` | 반증 단계 | `--reproduction-file` |
| `finalize` | 게시 분류 확정 | `--verification-file` |
| `comment` | Gitea 댓글 생성/갱신 | 토큰은 `GITEA_REVIEW_TOKEN` 환경 변수로만 |
| `reasoning` | 적용 중인 추론 수준 출력 | |

저장소를 읽거나 실행하는 명령(`index`, `evidence`, `impact`, `review`, `reproduce`)은 실제
`HEAD`가 `--head-sha`와 다르면 실행을 거부합니다. 단계 사이의 JSON은 모두 head SHA를
포함하고, 다른 SHA의 파일은 거부합니다. 실행 예시는 워크플로 파일에 있습니다.

## 개발

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

- [`tests/fixtures/shop`](tests/fixtures/shop)은 base 커밋으로 쓰는 예제 Django 앱이고,
  [`tests/fixtures/shop_head`](tests/fixtures/shop_head)를 덮어쓰면 head 커밋이 됩니다.
  차분 실행과 영향 분석 테스트는 이 두 커밋으로 임시 git 저장소를 만들어 실제 Django와
  SQLite에서 실행합니다.
- `gitnexus`가 PATH에 있으면 실제 GitNexus 인덱스와 MCP 서버로 정적 그래프 비교까지
  검증하는 테스트가 추가로 실행됩니다.
- Codex와 Gitea API는 테스트에서 mock 처리합니다.

## 알려진 한계

- ORM 필드 참조는 `Model.objects…`로 시작하는 체인만 해석합니다. 관계 매니저
  (`product.orders.filter(…)`)나 인스턴스 속성 대입은 아직 인식하지 않습니다.
- DRF serializer의 `source=`와 Celery task 경로는 아직 수집하지 않습니다.
- 실행 추적은 메인 스레드만 기록합니다.
- 마이그레이션이나 의존성이 바뀐 PR은 차분 실행 대신 단정 모드로 판정합니다.
- 차분 실행은 행이 5,000건을 넘는 테이블을 부분 비교합니다. 이때는 새로 생성된 행만
  비교합니다.

## 참고 자료

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) · [Codex 인증](https://learn.chatgpt.com/docs/auth) · [Codex MCP 설정](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [GitNexus](https://github.com/abhigyanpatwari/GitNexus)
- [Gitea Actions](https://docs.gitea.com/usage/actions/)
