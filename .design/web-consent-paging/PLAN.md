# 웹 분석 동의 진입·페이징·분석 0건 사유 구현 계획

> 상태: **완료** (Q1 A, Q2 A, Q3 A, Q4 A — 2026-11-02) · 기준 커밋: `5a781c4` (feat/python-runtime, PR #33 위) · 규모: 표준 · 고위험: 아니오 (스키마·동시성 변경 없음)

## 0. 진단 — 분석이 안 되는 이유
- 추적 저장소는 `github.com/nowgnas/skim` 1개, `analysis_policy=inherit`, 조직 정책 0건 → `analysis_allowed()`가 거부 (`lib/skim/db.py:172-182`).
- 자동·수동 분석은 허용 안 된 스냅샷을 조용히 건너뛴다 (`lib/skim/analyze/__init__.py:91`). 결과 메시지는 "분석 대상 0개 — 허용된 미분석 스냅샷 없음"뿐이라 원인이 안 보인다 (`lib/skim/jobs.py:180`). 실제 DB: analyze 3회 모두 0개, analyses 0건, 열린 PR 2·스냅샷 2.
- 허용 화면 `/settings`는 DB의 저장소 **425개 전체**를 정책 폼과 함께 한 페이지에 나열한다 (`lib/skim/web_settings.py:133`). 추적 중인 1개를 찾기 어렵다.
- doctor의 Codex 격리(codex-home·version·flags·probe)는 모두 정상 → 원인 아님.

## 1. 요구사항
- **목표**
  1. PR 상세에서 해당 저장소의 분석 허용(동의)을 바로 받는다. 기존과 같은 경고·확인 체크박스·CSRF 경계를 쓴다.
  2. `/` PR 목록, `/settings` 저장소 정책 목록, `/repos` 추적 목록·불러온 후보 목록에 페이징을 둔다.
  3. 분석 결과 메시지가 0건 사유(허용 안 됨 n, draft n)를 구분한다.
- **비목표**: 허용 즉시 분석 자동 실행(동의와 분석 요청은 분리 유지), 조직 정책 화면 개편, 스키마 변경, 저장소 425개 정리.
- **성공 기준**
  - [x] 허용 안 된 저장소의 PR 상세에 확인 체크박스가 있는 허용 폼이 보이고, 제출하면 정책이 allow가 되며 같은 PR 상세로 돌아온다 · `python -m unittest tests.test_web_settings`
  - [x] 체크박스 없이 제출하면 거부되고 정책은 그대로다 · 동일
  - [x] 돌아갈 주소는 `/pr/<숫자>`만 허용, 그 외(`//evil`, `http://…`)는 `/settings`로 · 동일
  - [x] 목록 25개 초과 시 페이지당 20개, 이전/다음 링크가 필터를 유지, 범위 밖 page는 마지막 페이지로 · `tests.test_web*`
  - [x] `/settings`는 추적·명시 정책·열린 PR 있는 저장소를 먼저, 이름 검색(`q`) 지원 · 동일
  - [x] 허용 안 된 스냅샷만 있을 때 결과가 "분석 대상 0개 — 허용 안 됨 1" 형태 · `tests.test_web_jobs` 또는 `test_analyze`

## 2. 정책 점검
| 영역 | 판정 | 내용 | 근거 / 질문 |
|---|---|---|---|
| 동의 의미 | ❓ | "에이전트 허용" = 저장소 코드를 Codex로 보내는 분석 허용으로 해석 | Q1 |
| 동의 범위 | ⚠️ | PR 상세에서는 그 저장소만 허용. 조직 허용은 기존 `/settings`에서 | Q2 |
| 동의 경계 | ✅ | 같은 `/settings/analysis` POST·`confirm=yes`·CSRF·Origin 검사 재사용, 새 POST 경로 없음 | `web_settings.py:101-106`, `web.py:344-372` |
| 동의 ≠ 분석 | ✅ | 허용 후 분석은 사용자가 "이 PR 분석"을 따로 누름 | `web_jobs.py:89-90`, 설계 원칙 |
| 리다이렉트 | ✅ | 열린 리다이렉트 방지: `/pr/<int>`만 | 신규 |
| 페이지 크기 | ⚠️ | 20개 고정, 설정화하지 않음 | Q3 |
| `/settings` 정렬 | ⚠️ | 추적·정책 지정·열린 PR 보유 저장소 우선 + 검색 | Q4 |

## 3. 열린 질문
| # | 질문 | 선택지 | 추천 | 답 |
|---|---|---|---|---|
| Q1 | "에이전트 허용"이 무엇인가 | A 저장소 코드의 Codex 전송 허용(분석 동의) / B Codex 격리 초기화·probe를 웹에서 | A — doctor상 B는 이미 정상이고 실제 차단 원인은 A | |
| Q2 | PR 상세에서 허용 범위 | A 저장소만 / B 저장소+조직 선택 | A — 조직 허용은 다른 저장소까지 전송하므로 설정 화면에서 명시적으로 | |
| Q3 | 페이지 크기 | A 20 고정 / B config 키 추가 | A — 설정 키는 모든 환경 문서까지 바뀜, 필요해지면 추가 | |
| Q4 | `/settings` 목록 | A 관련 저장소 우선+검색+페이징 / B 추적·정책 지정 저장소만 표시 | A — 리뷰 요청 저장소도 허용할 수 있어야 함 | |

## 4. 코드 맥락
- **레퍼런스**: 정책 폼 `web_settings.policy_form`·`mutate('/settings/analysis')`, 목록 `web.listing`, 상세 패널 `web_jobs.analysis_panel`.
- **컨벤션**: 문자열 HTML 조립 + `esc`, 폼은 `form()` 헬퍼, POST 후 303 리다이렉트 경로를 `mutate`가 반환, 테스트는 `unittest` + 실제 핸들러 요청(`tests/test_web_settings.py`의 `self.request`).
- **영향 범위**: `mutate` 반환값(리다이렉트), `fields()` 허용 필드, `listing` 출력, `/settings`·`/repos` 렌더, `Runner.run_analyze` 결과 문자열. `Analyzer.run()` 반환 형식은 유지.

## 5. 설계
> 사다리: 표준 라이브러리(`urllib.parse.urlencode`)로 페이지 링크 생성. 새 의존성·JS 없음(CSP `script-src 'self'` 유지).

### 변경 파일
| 구분 | 경로 | 내용 |
|---|---|---|
| 신규 | `lib/skim/paging.py` | `paginate(items, query, size=20) -> (slice, nav_html)`; page 파싱·클램프·필터 유지 링크 |
| 수정 | `lib/skim/web.py` | `listing`에 페이징 |
| 수정 | `lib/skim/web_jobs.py` | `analysis_panel`의 미허용 분기에 저장소 허용 폼(`next=/pr/<id>`) |
| 수정 | `lib/skim/web_settings.py` | `policy_form`에 `next` 옵션, `mutate`가 검증된 `next`로 리다이렉트, `/settings` 정렬·검색·페이징, `/repos` 두 목록 페이징 |
| 수정 | `lib/skim/analyze/__init__.py` | `self.skipped` 카운터(not_allowed·draft) 기록, 반환값 불변 |
| 수정 | `lib/skim/jobs.py` | 0건/결과 메시지에 skipped 사유 추가 |
| 수정 | `tests/test_web_settings.py`, `tests/test_web_jobs.py`(또는 해당 파일) | 성공 기준 테스트 |
| 수정 | `README.md` | 분석 허용 위치 안내 1줄 |

### API 계약
```
POST /settings/analysis   (기존)
요청: csrf, host, scope, target, policy, confirm[, next]   ← next 선택 필드 추가
응답: 303 Location = next가 ^/pr/[0-9]+$ 이면 next, 아니면 /settings
에러: 기존과 동일 (allow인데 confirm≠yes → 400)
GET /, /settings, /repos  ?page=N (선택), /settings ?q=검색어(선택)
```
하위 호환: next·page·q 없으면 기존 동작과 같다.

### 데이터 변경
없음.

### 트랜잭션 · 동시성 · 실패 처리
기존 정책 저장 트랜잭션을 그대로 쓴다. 분석 잠금·예산 경로 변경 없음.

## 6. 테스트 계획
| 계층 | 시나리오 | 종류 | 명령 |
|---|---|---|---|
| 웹 | 상세의 허용 폼 표시·제출·리다이렉트·confirm 누락 거부·악성 next | 통합(핸들러) | `python -m unittest discover -s tests` |
| 웹 | 목록 45개 → 1/3페이지, 필터 유지 링크, page=99·page=abc | 단위+통합 | 동일 |
| 웹 | /settings 우선순위·q 검색 | 통합 | 동일 |
| 작업 | 미허용 스냅샷만 있을 때 결과 문구 | 단위 | 동일 |

## 7. 구현 순서
- [x] 1. 성공 기준 테스트 작성 → 실패 확인
- [x] 2. paging.py + listing
- [x] 3. 동의 폼·next 리다이렉트
- [x] 4. /settings·/repos 페이징·정렬·검색
- [x] 5. 0건 사유 메시지
- [x] 6. 전체 테스트·로컬 데모 확인·독립 리뷰

---

## 9. 결과
- **한 줄 요약**: 분석 0건 원인(저장소 미동의)을 화면에 드러내고, PR 상세에서 저장소 단위 동의를 받으며, 목록 3곳에 20개 페이징을 넣었다.
- **핵심 변경**
  - `lib/skim/paging.py` — `paginate(items, query, path, key=)`: page 파싱(ASCII 숫자 9자리 이하), 마지막 페이지로 클램프, csrf 제외 쿼리 유지 링크
  - `lib/skim/web_settings.py` — `consent_form`(scope=repo 고정), `safe_next`(`/pr/<int>`만), `/settings` 정렬·검색·페이징, `/repos` `page`·`found` 페이징
  - `lib/skim/web_jobs.py` — 미동의면 동의 폼, 명시 거부(저장소 또는 상속 조직)면 설정 안내만
  - `lib/skim/db.py` — `analysis_denied` 추가, 조직 정책 조회를 `_org_policy`로 공유
  - `lib/skim/analyze/__init__.py` · `lib/skim/jobs.py` — `skipped`(not_allowed·draft·stale) 집계와 결과 문구
- **설계와 달라진 점**
  - `policy_form`에 `next`를 붙이지 않고 `consent_form`을 따로 만들었다. 상세 화면에서 scope·policy를 고정해야 해서 범용 폼과 분리했다.
  - 결과 문구 형식: `분석 대상 0개 건너뜀: 분석 허용 안 됨 N (PR 상세 또는 설정에서 저장소 분석을 허용하세요)`. 건너뛴 것도 결과도 없으면 기존 `허용된 미분석 스냅샷 없음`을 유지한다.
  - 리뷰 ❔1 반영: 명시 거부 저장소·조직에는 허용 폼을 다시 보이지 않는다(보수적 기본값, 사용자 확인 필요).
  - 리뷰 #3 반영: SHA 불일치(최신 수집 전)도 사유로 센다.

### 검증
| 항목 | 명령 | 결과 |
|---|---|---|
| 전체 테스트 | `PYTHONPATH=lib python3.11 -m unittest discover -s tests` | 267건 OK |
| 정렬 테스트 변이 검사 | 정책 정렬 조건 제거 후 `test_web_consent_paging` | FAIL 확인 후 원복 |
| 공백 검사 | `git diff --check` | OK |
| 실제 DB 복사본 | 오프라인 `skim web` + curl | `/settings` 425개 → 22페이지, 첫 행 `nowgnas/skim`; 동의 POST 303 → `/pr/2`, 복사본 정책 allow, 원본 inherit 유지 |

실행하지 못한 검증과 이유: 동의 후 실제 Codex 분석은 실행하지 않음(코드 외부 전송, 사용자 결정 필요). 브라우저 렌더링은 curl로만 확인.

### 독립 리뷰
> 리뷰어: impl-reviewer · 🔴 0 · 🟠 1 반영(테스트 보강) · 🟡 5 중 #2·#3·#4·#6 반영, #5 보류 · ⚪ 2 영향 없음

### 후속 작업 · 발견한 문제
- #5 보류: GET 필터·검색 폼이 csrf hidden을 URL에 싣는다. `test_all_forms_have_hidden_csrf_and_escaped_values`가 모든 폼에 csrf를 강제하므로 기존 `/` 필터 폼과 함께 별도 변경으로 정리한다.
- `skim web`은 `SKIM_HOME` 경로가 길면 `AF_UNIX path too long`으로 시작하지 못한다(`lib/skim/web.py` `web.sock`).
