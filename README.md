# gitea-auto-reviewer

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![stdlib only](https://img.shields.io/badge/runtime%20deps-stdlib%20only-informational.svg)](pyproject.toml)

**GitNexus가 생성한 의존성 정적 분석 그래프를 Codex와 연동해 변경 영향 경로를
추적하고, 코드 변경으로 발생할 수 있는 데이터 정합성 문제를 실제 테스트 DB에서
재현·검증하는 셀프 호스팅 Gitea PR 리뷰 소프트웨어입니다.**

대부분의 AI PR 리뷰어는 diff를 읽고 LLM의 의견을 그대로 게시합니다.
`gitea-auto-reviewer`는 GitNexus의 저장소 의존성 그래프로 변경된 코드의 호출자,
피호출자 및 관련 프로세스를 추적합니다. 이어서 가능한 발견 사항을 강제 롤백
트랜잭션 안의 실제 테스트 데이터베이스에서 다시 실행합니다. 재현·검증에 성공한
항목은 `재현된 문제`, DB 재현 대상이 아니거나 결과가 불확실한 항목은
`정적 분석 발견 사항(미재현)`으로 구분해 PR 댓글에 게시하며, 실행으로 반증된
항목만 제외합니다.

기존 Codex 구독을 셀프 호스팅 Gitea의 풀 리퀘스트 댓글과 연결해 다음 내용을
검증합니다.

- 변경으로 데이터의 생성·수정·조회 흐름에 새로운 전제가 생기는지
- 기존 호출 경로에서 누락값, 경계값 또는 상태 불일치가 발생하는지
- DB 스키마와 애플리케이션 로직, API, SCM/ERP 연동 사이의 정합성이 유지되는지
- 변경이 운영 환경에 미칠 수 있는 영향과 사람이 확인해야 할 항목

OpenAI API 키나 Copilot 라이선스는 필요하지 않습니다. 이 도구는 AI가 지원하는 1차
리뷰어이지 병합 게이트가 아닙니다. 일반 PR 댓글 하나를 생성하거나 업데이트할 수
있지만 승인, 변경 요청, 병합, 커밋 푸시 또는 브랜치 보호 규칙 변경은 할 수 없습니다.

## 리뷰 및 재현 흐름

```text
PR head ── 기존 Windows 테스트 러너
              ├─ 정확한 head SHA에서 GitNexus 분석
              ├─ Django 검사
              ├─ 마이그레이션 검사
              ├─ pytest
              └─ head SHA에 연결된 evidence.json
                          │
신뢰할 수 있는 base 워크플로의 pull_request_target
              │
              ├─ 동일 저장소 PR인지 확인
              ├─ 읽기 전용 검사를 위해 PR head 체크아웃
              ├─ base 객체 가져오기(PR 코드 실행 안 함)
              ├─ git diff base...head
              └─ base:AI_REVIEW.md 읽기
                          │
                     리뷰 프로세스
                     Codex 인증: 있음
                     Gitea 토큰: 없음
                     샌드박스: 읽기 전용
                     GitNexus MCP: 있음
                          │
                 candidate-review.json
                          │
                 Codex 재현 계획(medium)
                 후보가 없으면 Codex 호출 생략
                          │
               Django view/ORM 직접 호출
               테스트 DB + transaction.atomic()
               강제 롤백 + 정리 상태 검사
               선택적 자연 데이터 조건 집계
                          │
                Codex 반증 단계(low)
                재현 증거 읽기
                          │
                 검증 결과에 따라 분류
                 ├─ 재현·검증 성공: 재현된 문제
                 ├─ 미실행/불확실: 정적 분석 발견 사항(미재현)
                 └─ 실행으로 반증: 제외
                     review.json
                          │
                     댓글 프로세스
                     Codex 인증: 사용 안 함
                     Gitea 토큰: 있음
                          │
                  PR 댓글 생성/업데이트
                  같은 SHA의 재현 결과 보존
```

### 최초 FINDING: Codex + GitNexus 정적 분석

최초 FINDING 단계는 재현 단계가 아니라 읽기 전용 정적 분석입니다. Codex는 다음
입력을 함께 사용해 PR이 새로 만들거나 악화한 문제 후보를 찾습니다.

- `base...head` 전체 diff와 PR head의 전체 저장소
- GitNexus가 인덱싱한 심볼, 호출자, 피호출자 및 관련 프로세스 그래프
- head SHA에 연결된 Django, 마이그레이션 및 pytest 결과
- base 커밋의 신뢰된 `AI_REVIEW.md` 정책

프로그램은 Codex CLI를 JSONL 이벤트 모드로 실행하고, 초안 생성을 완료하기 전에
GitNexus의 다음 도구가 실제로 성공했는지 검사합니다.

- `detect_changes`: 전체 변경 심볼 탐지
- `context`: 변경 심볼의 정의와 관계 확인
- `impact`: 직접·간접 영향 경로 확인

세 호출 중 하나라도 완료되지 않으면 리뷰를 실패시키며 조용히 빈 FINDING으로
처리하지 않습니다. 실행 경로가 불명확할 때 사용하는 `trace`는 선택 항목입니다.

Codex는 변경 전후 조건과 경계값을 비교하고, 새로 허용된 상태가 데이터 생성·수정·
삭제·분할·계산 및 MES/SCM/ERP 경로로 전달되는지 추적합니다. FINDING은 `bug`,
`security`, `performance`, `dependency`, `policy` 범주로 최대 5개까지 생성할 수
있습니다. 모든 FINDING은 구체적인 운영 영향과 실제 `file:line` 근거가 필요하며,
프로그램은 해당 파일과 줄 및 정책 인용이 실제로 존재하는지 검증합니다.

이 단계에서는 프로젝트 코드나 테스트를 실행하지 않습니다. 생성된 후보는 이후
`plan → reproduce → verify → finalize` 단계에서 DB 재현 가능 여부에 따라 `재현된
문제` 또는 `정적 분석 발견 사항(미재현)`으로 분류됩니다.

CLI 단계는 하나의 Windows 작업에서 순차적으로 실행됩니다.

```bash
gitea-auto-reviewer index ...     # PR head 코드 그래프 생성/업데이트
gitea-auto-reviewer evidence ...  # 신뢰된 내부 PR 실행 및 증거 수집
gitea-auto-reviewer review ...    # Codex 리뷰, Gitea 자격 증명 없음
gitea-auto-reviewer plan ...      # 재현 가능한 발견 사항의 재현 계획 수립
gitea-auto-reviewer reproduce ... # CI Python으로 실행 후 DB 강제 롤백
gitea-auto-reviewer verify ...    # Codex가 재현 결과 반증 시도
gitea-auto-reviewer finalize ...  # 확인되고 정리 검증을 통과한 결과만 유지
gitea-auto-reviewer comment ...   # Gitea 댓글 작성, Codex나 PR 코드 실행 안 함
```

Codex 자식 프로세스에는 러너의 전체 환경 대신 허용 목록의 환경 변수만 전달됩니다.
PR head 저장소에서 `--sandbox read-only`, `--ephemeral`,
`--ignore-user-config`, `--ignore-rules`로 실행되며, 전체 저장소를 읽어 변경이
호출자, serializer, 연동, 배포 파일 및 테스트로 어떻게 이어지는지 추적합니다.
프롬프트는 `AGENTS.md`를 포함한 모든 저장소 파일을 신뢰할 수 없는 데이터로 취급하고
프로젝트 코드 실행을 금지합니다.

`read-only`는 저장소 쓰기를 막지만 운영체제 수준에서 프로세스 실행까지 차단하지는
않습니다. 프로세스를 절대 실행할 수 없는 경계가 필요하다면 저장소를 읽기 전용으로
마운트한 별도 강화 샌드박스에서 리뷰 단계를 실행해야 합니다.

## 지원 범위와 신뢰 모델

v0.2는 다음 환경만 지원합니다.

- 신뢰할 수 있는 영구 셀프 호스팅 러너
- 비공개 또는 내부 저장소
- head와 base가 같은 저장소에 속한 PR
- 사전에 설치하고 감사한 이 패키지 버전
- 일반 Markdown 타임라인 댓글
- 기본 최대 10MB 크기의 diff

PR 코드는 증거 수집 프로세스에서만 실행됩니다. Codex에는 테스트, 빌드,
마이그레이션, 패키지 관리자 또는 프로젝트 스크립트 실행을 요청하지 않습니다. 검사
프로세스에는 허용 목록 환경과 임시 HOME이 제공되므로 Gitea 자격 증명,
`CODEX_HOME` 및 관련 없는 러너 비밀 정보가 상속되지 않습니다.

이는 프로세스 분리일 뿐 계정 또는 VM 격리는 아닙니다. fork PR이나 신뢰할 수 없는
공개 저장소에는 사용하지 마세요.

## 요구 사항

- Python 3.11 이상
- Git
- `--output-schema`와 `exec --json`을 지원하는 Codex CLI
- GitNexus CLI
- `ai-review-windows` 라벨이 지정된 전용 Windows Gitea 러너
- Codex를 사용할 수 있는 ChatGPT 계정
- PR/이슈 댓글을 읽고 쓸 수 있는 Gitea 토큰

## 러너에 설치하기

기존 러너 서비스 계정에 감사가 끝난 릴리스를 설치합니다. 풀 리퀘스트에서 실행하지
말고 러너를 프로비저닝할 때 실행하세요.

```bash
python -m pip install "gitea-auto-reviewer==0.2.1"
```

패키지를 배포하기 전 개발 환경에서는 다음과 같이 설치합니다.

```bash
git clone https://github.com/hyunsuhahaha/gitea-auto-reviewer.git
cd gitea-auto-reviewer
python -m pip install .
python -m pip install uv
```

GitNexus는 전용 러너 계정으로 한 번만 설치합니다.

```powershell
npm install -g gitnexus
gitnexus --version
```

신뢰할 수 없는 PR 체크아웃에서 `pip install .`을 실행하지 마세요. Python 빌드 훅은
실행 가능한 코드입니다.

## API 키 없이 Codex 인증하기

Windows 러너를 실행하는 OS 계정으로 한 번 로그인합니다.

```bash
codex login
codex login status
```

**Sign in with ChatGPT**를 선택하세요. CLI가 세션을 캐시하고 해당 ChatGPT 계정의
Codex 사용 권한을 이용합니다. `OPENAI_API_KEY`는 읽거나 요구하지 않습니다. 캐시된
로그인이 만료되거나 취소되면 `codex login`을 다시 실행해야 합니다. 자세한 내용은
[Codex 인증 문서](https://learn.chatgpt.com/docs/auth)를 참고하세요.

## Gitea 설정하기

1. 전용 Gitea 봇 또는 서비스 계정을 생성합니다.
2. 토큰에는 이슈/PR 댓글 조회·생성·수정 권한만 부여합니다. 병합 또는 관리자 권한은
   부여하지 마세요.
3. 토큰을 저장소 또는 조직 Actions 비밀 정보 `AI_REVIEW_GITEA_TOKEN`으로
   저장합니다.
4. [`.gitea/workflows/ai-review.yml`](.gitea/workflows/ai-review.yml)을 리뷰 대상
   저장소의 신뢰할 수 있는 base 브랜치에 복사합니다.
5. 영구 리뷰어 가상 환경에 `uv`를 설치합니다. 각 PR은 새 `.venv-ci`를 사용하며,
   uv는 Windows 패키지 캐시를 재사용합니다.
6. 전용 러너 계정에서 `gitea-auto-reviewer`, `codex`, `gitnexus`를 사용할 수 있고
   러너에 `ai-review-windows` 라벨이 지정되어 있는지 확인합니다.

예제는 워크플로 정의를 신뢰할 수 있는 base 브랜치에서 가져오도록
`pull_request_target`을 사용합니다. 동일 저장소 PR인지 확인한 뒤 새 CI 환경에서
결정론적 검사를 실행하고, Codex는 읽기 전용이자 자격 증명 없이 실행합니다.

## CLI

리뷰 전에 체크아웃된 PR head를 인덱싱합니다. 실제 `HEAD`가 전달된 SHA와 다르면
명령이 실행을 거부합니다.

```powershell
gitea-auto-reviewer index `
  --head-sha 2222222222222222222222222222222222222222 `
  --repo-dir C:\runner\work\payments `
  --gitnexus-binary C:\Users\GiteaAIReview\AppData\Roaming\npm\gitnexus.cmd
```

결정론적 증거를 수집합니다.

```powershell
gitea-auto-reviewer evidence `
  --head-sha 2222222222222222222222222222222222222222 `
  --repo-dir C:\runner\work\payments `
  --output C:\runner\temp\evidence.json
```

이 명령은 `python manage.py check`, `python manage.py makemigrations --check
--dry-run`, `python -m pytest -q -p no:cacheprovider`를 실행합니다. 실행 전에 실제
`HEAD`가 `--head-sha`와 같은지 확인하고 해당 SHA를 증거 문서에 기록합니다.

예제 워크플로는 세 검사를 별도 Actions 단계로 실행한 뒤 SHA에 연결된 JSON 파일을
`evidence-merge`로 합칩니다. 첫 Codex 단계와 재현 계획은 medium 추론 수준을,
독립적인 반증 단계는 low 수준을 사용합니다. 후보가 없으면 재현 계획 호출을
생략합니다.

다음 Gitea Actions 저장소 변수로 기본값을 바꿀 수 있습니다.

- `AI_REVIEW_FIRST_PASS_EFFORT` (기본값: `medium`)
- `AI_REVIEW_PLAN_EFFORT` (기본값: `medium`)
- `AI_REVIEW_VERIFY_EFFORT` (기본값: `low`)

개별 호출은 `--reasoning-effort`로 재정의할 수 있습니다.
`gitea-auto-reviewer reasoning`은 적용되는 설정을 출력합니다.

구조화된 리뷰를 생성합니다.

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

두 Codex 단계는 같은 GitNexus STDIO MCP 서버를 사용합니다. 변경된 심볼, 컨텍스트,
영향 및 관련 프로세스를 조회하며, 정적 변경 영향 증거를 실제 저장소 파일과 결정론적
CI 증거에 대조합니다.

게시 전에 롤백 전용 재현을 생성하고 실행합니다.

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

고정 실행기는 Django를 초기화하고 필수 런타임 설정을 확인한 뒤 모든 Django DB에서
`transaction.atomic()`을 열어 강제로 롤백합니다. DB 연결을 닫고 새 연결에서 행과
필드를 다시 검사합니다. 정리 검증을 통과한 `confirmed` 결과만 `재현된 문제`에
표시됩니다. 재현 계획 제외, 시간 초과·예외, 정리 미검증 및 2차 검증 미채택 사례는
각 사유와 함께 정적 분석 항목에 남고, 실행으로 반증된 사례만 제외됩니다. PostgreSQL
시퀀스는 트랜잭션 대상이 아니므로 값에 빈 구간이 생길 수 있습니다.

재현 사례 수에는 제한이 없습니다. 가능한 경우 실제 ORM 모집단을 집계해
`버그 조건 충족률: 포장 투입 버킷 467/2,481건 (18.82%)`과 같은 비율도 표시합니다.
이 수치는 정보 제공용이며 게시 여부를 결정하지 않습니다.

봇 댓글을 게시하거나 업데이트합니다.

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

같은 값은 `GITEA_REPOSITORY`, `GITEA_HEAD_REPOSITORY`, `GITEA_PR_NUMBER`,
`GITEA_PR_TITLE`, `GITEA_BASE_SHA`, `GITEA_HEAD_SHA`, `GITEA_URL`,
`GITEA_REVIEW_TOKEN`으로 전달할 수도 있습니다. 토큰은 프로세스 목록이나 셸 기록에
나타나지 않도록 환경 변수로만 받습니다.

### 기존 PR 또는 병합된 PR을 수동으로 리뷰하기

예제 워크플로는 `workflow_dispatch`도 지원합니다. Actions 페이지에서 **Codex AI
review**와 **Run workflow**를 선택하고 기존 PR 번호만 입력하세요. Gitea에서 원래
제목과 SHA를 읽어 같은 리뷰 단계를 실행하므로 PR을 다시 열거나 테스트 커밋을 만들지
않아도 됩니다. 원래 head 커밋은 Gitea 저장소에 남아 있어야 합니다.

중간 JSON 파일은 마지막 `always()` 단계에서
`C:\gitea\ai-review\debug-runs\<GITHUB_RUN_ID>`에 복사됩니다.

## 변경 영향 형식

PR 댓글에는 검증된 사실, 주요 변경, 재현된 문제 및 영향 파일을 간결하게 표시합니다.

```text
변경 파일        7개
DB 스키마 변경   있음
데이터 처리 변경 있음
API Contract     변경
외부연동         영향 가능

검증된 사실
✅ Django check PASS
✅ Migration check 누락 없음
✅ 테스트(pytest) 147/147 PASS

위험도           🟠 HIGH · 근거 HIGH

재현된 문제
• 기존 Product 생성 경로가 remark를 전달하지 않아 생성 요청이 실패함
  영향: 기존 상품 등록 경로에서 상품 생성이 중단됨
  버그 조건 충족률: 기존 상품 생성 경로 4/12건 (33.33%)
  관찰 결과: 필수 필드 오류 응답 확인
  롤백 검증: 통과
  └ product/models.py:31
  └ product/services.py:18
```

변경 파일 수와 경로는 Git에서 가져오고 Django, 마이그레이션 및 pytest 결과는 증거
문서에서 가져와 모델 출력을 덮어씁니다. `head_sha`가 PR head와 정확히 일치하지 않는
증거는 거부합니다. 숨겨진 마커로 기존 댓글을 업데이트합니다.

```html
<!-- gitea-auto-reviewer:pr=42:sha=abc123... -->
```

같은 PR head SHA를 다시 실행하면 이전에 재현된 발견 사항을 새 결과와 병합합니다.
다른 SHA는 새 결과 집합을 시작합니다.

구체적인 버그, 보안, 성능, 의존성 문제 및 명시적인 base 정책 위반만 게시합니다.
스타일, 이름 짓기 및 일반적인 개선 의견은 제외합니다. 각 항목의 파일, 줄 범위와 정책
인용을 검증하며 결정론적 CI 사실을 우선합니다. 독립적인 두 번째 Codex 단계는 재현된
발견 사항을 그대로 유지하거나 삭제할 수만 있고 추가하거나 다시 쓸 수 없습니다.

`영향 파일`에는 GitNexus로 관계가 확인된 미변경 파일을 최대 5개까지 표시합니다.
변경된 파일, 일반 유틸리티, import로만 연결된 항목 및 검증되지 않은 관계는
제외합니다.

## 프로젝트 정책

기본 브랜치의 `AI_REVIEW.md`에 운영 위험과 도메인별 리뷰 우선순위를 정의할 수
있습니다. 리뷰어는 항상 PR의 **base 커밋**에서 이 파일을 읽습니다.

```text
base 커밋의 AI_REVIEW.md   -> 현재 리뷰 정책
PR에서 변경한 AI_REVIEW.md -> 이번 리뷰에서는 무시
병합된 AI_REVIEW.md         -> 이후 PR부터 적용
```

따라서 PR이 자기 자신을 리뷰하는 지침을 바꿀 수 없습니다.

## 증거 경계와 향후 입력

컨텍스트에는 읽기 전용 저장소, GitNexus 코드 그래프, diff, base 정책 및 격리된
CI에서 가져온 SHA 연결 증거가 포함됩니다. v0.2는 Django 검사, 마이그레이션 검사,
pytest를 지원합니다. 향후 정규화된 Ruff와 Semgrep 결과를 추가할 수 있습니다.

## 개발

```bash
python -m pip install -e ".[dev]"
pytest
```

테스트에서는 Git, Codex 및 Gitea를 mock 처리하므로 네트워크 접근, 자격 증명 또는
실행 중인 Gitea 인스턴스가 필요하지 않습니다.

## 참고 자료

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Codex 인증](https://learn.chatgpt.com/docs/auth)
- [Codex MCP 설정](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [GitNexus](https://github.com/nxpatterns/gitnexus)
- [Gitea Actions](https://docs.gitea.com/usage/actions/)
- [Gitea Actions 보안 지침](https://docs.gitea.com/usage/actions/overview/)
